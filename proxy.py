"""Experimental TCP relay; not intended for production mining."""
import argparse
import asyncio
import logging
import ssl
import json
import time
import math
import socket
import errno
import ipaddress
from observer import Metrics, Observer

LOG = logging.getLogger("mining_proxy")


class Relay:
    def __init__(self, host, port, *, tls=False, limit=512, timeout=10, backup=None, tls_context=None):
        if not host or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("invalid primary pool")
        if not isinstance(limit, int) or not 1 <= limit <= 10000:
            raise ValueError("connection limit must be 1..10000")
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0.1 <= timeout <= 120:
            raise ValueError("connection timeout must be 0.1..120 seconds")
        if backup and (not backup[0] or not isinstance(backup[1], int) or not 1 <= backup[1] <= 65535):
            raise ValueError("invalid backup pool")
        self.host, self.port, self.tls = host, port, tls
        self.limit, self.timeout = limit, timeout
        self.active = 0
        self.tasks = set()
        self.metrics = Metrics()
        self.backup = backup
        self.sessions = {}
        self.next_session_id = 0
        self.listeners = []
        if tls_context is not None and not tls:
            raise ValueError("TLS context provided while TLS is disabled")
        self.tls_context = (tls_context or ssl.create_default_context()) if tls else None
        if self.tls_context and (not self.tls_context.check_hostname or
                                 self.tls_context.verify_mode != ssl.CERT_REQUIRED):
            raise ValueError("TLS requires hostname and certificate verification")

    async def handle(self, reader, writer):
        if self.active >= self.limit:
            self.metrics.refused_capacity += 1
            writer.close()
            await writer.wait_closed()
            return
        self.active += 1
        self.metrics.active += 1
        self.metrics.connections += 1
        self.next_session_id += 1
        session_id = self.next_session_id
        session_metrics = Metrics(parent=self.metrics)
        session = {"id": session_id, "state": "connecting", "worker": None,
                   "pool": None, "started": time.monotonic(), "metrics": session_metrics}
        self.sessions[session_id] = session

        def authorized(name):
            session["worker"] = "".join(c if c.isprintable() else "?" for c in name)

        observer = Observer(session_metrics, on_authorized=authorized)
        session["observer"] = observer
        task = asyncio.current_task()
        self.tasks.add(task)
        upstream = None
        pumps = []
        try:
            endpoints = [(self.host, self.port)] + ([self.backup] if self.backup else [])
            for index, endpoint in enumerate(endpoints):
                try:
                    if await self.is_self_route(endpoint):
                        self.metrics.blocked_loops += 1
                        self.metrics.upstream_failures += 1
                        continue
                    remote_reader, upstream = await asyncio.wait_for(
                        asyncio.open_connection(*endpoint, ssl=self.tls_context), self.timeout
                    )
                    session["state"] = "connected"
                    session["pool"] = "primary" if index == 0 else "backup"
                    if index:
                        self.metrics.backup_connections += 1
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
            if len(done) == 2:
                self.metrics.closed_ambiguous += 1
            elif pumps[0] in done:
                self.metrics.closed_by_miner += 1
            else:
                self.metrics.closed_by_pool += 1
        except (OSError, asyncio.TimeoutError, ssl.SSLError):
            self.metrics.transport_errors += 1
            LOG.info("Connection ended or upstream unavailable")
        finally:
            try:
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
                            await asyncio.wait_for(stream.wait_closed(), 3)
                        except (OSError, asyncio.TimeoutError, ssl.SSLError):
                            pass
            finally:
                # Counters and registry must recover even if cleanup is cancelled.
                for stream in (upstream, writer):
                    if stream:
                        stream.close()
                self.active -= 1
                self.metrics.active -= 1
                observer.close()
                self.sessions.pop(session_id, None)
                self.tasks.discard(task)

    def set_listeners(self, server):
        self.listeners = [(item.family, item.getsockname()) for item in server.sockets]

    async def is_self_route(self, endpoint):
        relevant = [(family, address) for family, address in self.listeners if address[1] == endpoint[1]]
        if not relevant:
            return False
        loop = asyncio.get_running_loop()
        addresses = await asyncio.wait_for(loop.getaddrinfo(endpoint[0], endpoint[1], type=socket.SOCK_STREAM), self.timeout)
        for family, _, _, _, remote in addresses:
            for listen_family, local in relevant:
                if family != listen_family:
                    continue
                remote_ip = ipaddress.ip_address(remote[0].split("%", 1)[0])
                local_ip = ipaddress.ip_address(local[0].split("%", 1)[0])
                if remote_ip == local_ip:
                    return True
                if local_ip.is_unspecified:
                    # Binding the target IP establishes whether it belongs to this host.
                    candidate = socket.socket(family, socket.SOCK_STREAM)
                    try:
                        bind_address = (remote[0], 0) if family == socket.AF_INET else (remote[0], 0, 0, remote[3])
                        candidate.bind(bind_address)
                        return True
                    except OSError as exc:
                        if exc.errno not in (errno.EADDRNOTAVAIL, 10049):
                            raise
                    finally:
                        candidate.close()
        return False

    def session_snapshot(self):
        now = time.monotonic()
        return [{"id": item["id"], "state": item["state"], "worker": item["worker"],
                 "pool": item["pool"], "duration_seconds": max(0, int(now - item["started"])),
                 "submitted": item["metrics"].submitted, "accepted": item["metrics"].accepted,
                 "rejected": item["metrics"].rejected, "unmatched": item["metrics"].unmatched,
                 "pending": item["observer"].pending_shares()}
                for item in self.sessions.values()]

    async def shutdown(self):
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def run(args):
    backup = (args.backup_host, args.backup_port) if args.backup_host else None
    relay = Relay(args.pool_host, args.pool_port, tls=args.tls, backup=backup,
                  limit=args.max_connections, timeout=args.connect_timeout)
    server = await asyncio.start_server(relay.handle, args.listen_host, args.listen_port, backlog=512)
    relay.set_listeners(server)
    try:
        dashboard = await asyncio.start_server(lambda r, w: dashboard_request(r, w, relay.metrics, relay.session_snapshot),
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


async def dashboard_request(reader, writer, metrics, sessions=lambda: []):
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
        elif first[1] == b"/api/sessions":
            status, body, content_type = b"200 OK", json.dumps(sessions()).encode(), b"application/json"
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
<pre id="metrics">Загрузка…</pre><h2>Подключения</h2>
<p>Имя показано после успешного ответа на авторизацию. Несколько воркеров в одном соединении пока не отображаются отдельно.</p>
<pre id="sessions">Загрузка…</pre><script>
async function refresh(){try {let r=await fetch('/api/metrics');
let m=await r.json(), labels={active:'Активных подключений', connections:'Всего подключений',
refused_capacity:'Отклонено из-за лимита подключений',
blocked_loops:'Предотвращено очевидных петель',
backup_connections:'Подключений к резервному пулу',closed_by_miner:'Закрыто со стороны майнера',
closed_by_pool:'Закрыто со стороны пула',closed_ambiguous:'Одновременных закрытий',transport_errors:'Ошибок передачи',
upstream_failures:'Ошибок подключения к пулу',submitted:'Отправлено шар',accepted:'Принято шар',
rejected:'Отклонено шар',unmatched:'Ответ не сопоставлен',observation_errors:'Ошибки наблюдения'};
document.getElementById('metrics').textContent=Object.keys(labels).map(k=>labels[k]+': '+m[k]).join('\\n');
let s=await (await fetch('/api/sessions')).json();
document.getElementById('sessions').textContent=s.length?s.map(x=>
'#'+x.id+' | '+(x.worker||'ожидание авторизации')+' | '+x.state+' / '+(x.pool||'—')+
' | принято '+x.accepted+', отклонено '+x.rejected+', ожидают '+x.pending+', не сопоставлено '+x.unmatched).join('\\n'):'Нет подключений';}
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
    parser.add_argument("--max-connections", type=int, default=512, metavar="N")
    parser.add_argument("--connect-timeout", type=float, default=10, metavar="SECONDS")
    args = parser.parse_args()
    if bool(args.backup_host) != bool(args.backup_port):
        parser.error("--backup-host and --backup-port must be supplied together")
    if not 1 <= args.max_connections <= 10000:
        parser.error("--max-connections must be in 1..10000")
    if not math.isfinite(args.connect_timeout) or not 0.1 <= args.connect_timeout <= 120:
        parser.error("--connect-timeout must be in 0.1..120 seconds")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
