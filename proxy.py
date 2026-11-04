"""Experimental TCP relay; not intended for production mining."""
import argparse
import asyncio
import logging
import ssl
import json
from observer import Metrics, Observer

LOG = logging.getLogger("mining_proxy")


class Relay:
    def __init__(self, host, port, *, tls=False, limit=512, timeout=10, backup=None):
        self.host, self.port, self.tls = host, port, tls
        self.limit, self.timeout = limit, timeout
        self.active = 0
        self.tasks = set()
        self.metrics = Metrics()
        self.backup = backup

    async def handle(self, reader, writer):
        if self.active >= self.limit:
            writer.close()
            await writer.wait_closed()
            return
        self.active += 1
        self.metrics.active += 1
        self.metrics.connections += 1
        observer = Observer(self.metrics)
        task = asyncio.current_task()
        self.tasks.add(task)
        upstream = None
        pumps = []
        try:
            context = ssl.create_default_context() if self.tls else None
            endpoints = [(self.host, self.port)] + ([self.backup] if self.backup else [])
            for endpoint in endpoints:
                try:
                    remote_reader, upstream = await asyncio.wait_for(
                        asyncio.open_connection(*endpoint, ssl=context), self.timeout
                    )
                    break
                except (OSError, asyncio.TimeoutError, ssl.SSLError):
                    self.metrics.upstream_failures += 1
            if upstream is None:
                return

            async def copy(source, destination, direction):
                while data := await source.read(65536):
                    destination.write(data)
                    try:
                        observer.feed(direction, data)
                    except Exception:
                        self.metrics.observation_errors += 1
                    await destination.drain()

            pumps = [asyncio.create_task(copy(reader, upstream, 0)),
                     asyncio.create_task(copy(remote_reader, writer, 1))]
            done, _ = await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
            for finished in done:
                finished.result()
        except (OSError, asyncio.TimeoutError, ssl.SSLError):
            LOG.info("Connection ended or upstream unavailable")
        finally:
            for pump in pumps:
                pump.cancel()
            if pumps:
                await asyncio.gather(*pumps, return_exceptions=True)
            for stream in (upstream, writer):
                if stream:
                    stream.close()
            for stream in (upstream, writer):
                if stream:
                    try:
                        await stream.wait_closed()
                    except (OSError, ssl.SSLError):
                        pass
            self.active -= 1
            self.metrics.active -= 1
            observer.close()
            self.tasks.discard(task)

    async def shutdown(self):
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def run(args):
    backup = (args.backup_host, args.backup_port) if args.backup_host else None
    relay = Relay(args.pool_host, args.pool_port, tls=args.tls, backup=backup)
    server = await asyncio.start_server(relay.handle, args.listen_host, args.listen_port, backlog=512)
    try:
        dashboard = await asyncio.start_server(lambda r, w: dashboard_request(r, w, relay.metrics),
                                               "127.0.0.1", args.dashboard_port)
    except BaseException:
        server.close()
        await server.wait_closed()
        raise
    LOG.warning("Experimental relay only; not for production. Listening on %s", server.sockets[0].getsockname())
    LOG.info("Dashboard: http://127.0.0.1:%s", args.dashboard_port)
    try:
        async with server, dashboard:
            await server.serve_forever()
    finally:
        server.close()
        dashboard.close()
        await server.wait_closed()
        await dashboard.wait_closed()
        await relay.shutdown()


async def dashboard_request(reader, writer, metrics):
    try:
        request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 3)
        first = request.split(b"\r\n", 1)[0].split()
        # An absolute URL or another Host can indicate a cross-site request.
        headers = request.lower().split(b"\r\n")
        hosts = [h for h in headers if h.startswith(b"host:")]
        sock = writer.get_extra_info("sockname")
        expected_host = ("host: 127.0.0.1:" + str(sock[1])).encode()
        if len(first) != 3 or first[0] != b"GET" or hosts != [expected_host]:
            status, body, content_type = b"403 Forbidden", b"Forbidden", b"text/plain"
        elif first[1] == b"/api/metrics":
            status, body, content_type = b"200 OK", json.dumps(metrics.snapshot()).encode(), b"application/json"
        elif first[1] == b"/":
            status, body, content_type = b"200 OK", DASHBOARD, b"text/html; charset=utf-8"
        else:
            status, body, content_type = b"404 Not Found", b"Not found", b"text/plain"
        writer.write(b"HTTP/1.1 " + status + b"\r\nContent-Type: " + content_type +
                     b"\r\nCache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\nContent-Length: " +
                     str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
        await writer.drain()
    except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        pass
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass


DASHBOARD = """<!doctype html><html lang="ru"><meta charset="utf-8">
<title>Mining Proxy — тестовый стенд</title><body>
<h1>Mining Proxy — тестовый стенд</h1>
<p>Данные приблизительные, только за время работы процесса. Не использовать для расчёта выплат.</p>
<pre id="metrics">Загрузка…</pre><script>
async function refresh(){try {let r=await fetch('/api/metrics');
let m=await r.json(), labels={active:'Активных подключений', connections:'Всего подключений',
upstream_failures:'Ошибок подключения к пулу',submitted:'Отправлено шар',accepted:'Принято шар',
rejected:'Отклонено шар',unmatched:'Ответ не сопоставлен',observation_errors:'Ошибки наблюдения'};
document.getElementById('metrics').textContent=Object.keys(labels).map(k=>labels[k]+': '+m[k]).join('\\n')}
catch(e){document.getElementById('metrics').textContent='Нет связи'} }
refresh();setInterval(refresh,3000);</script></body></html>""".encode("utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pool-host", required=True)
    parser.add_argument("--pool-port", type=int, required=True, choices=range(1, 65536), metavar="PORT")
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", type=int, default=3333, choices=range(1, 65536), metavar="PORT")
    parser.add_argument("--tls", action="store_true", help="Verify TLS certificate on upstream connection")
    parser.add_argument("--backup-host", help="Backup pool, used when initial connection fails")
    parser.add_argument("--backup-port", type=int, choices=range(1, 65536), metavar="PORT")
    parser.add_argument("--dashboard-port", type=int, default=8080, choices=range(1, 65536), metavar="PORT")
    args = parser.parse_args()
    if bool(args.backup_host) != bool(args.backup_port):
        parser.error("--backup-host and --backup-port must be supplied together")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
