"""Experimental TCP relay; not intended for production mining."""
import argparse
import asyncio
import logging
import ssl

LOG = logging.getLogger("mining_proxy")


class Relay:
    def __init__(self, host, port, *, tls=False, limit=128, timeout=10):
        self.host, self.port, self.tls = host, port, tls
        self.limit, self.timeout = limit, timeout
        self.active = 0
        self.tasks = set()

    async def handle(self, reader, writer):
        if self.active >= self.limit:
            writer.close()
            await writer.wait_closed()
            return
        self.active += 1
        task = asyncio.current_task()
        self.tasks.add(task)
        upstream = None
        pumps = []
        try:
            context = ssl.create_default_context() if self.tls else None
            remote_reader, upstream = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port, ssl=context), self.timeout
            )

            async def copy(source, destination):
                while data := await source.read(65536):
                    destination.write(data)
                    await destination.drain()

            pumps = [asyncio.create_task(copy(reader, upstream)),
                     asyncio.create_task(copy(remote_reader, writer))]
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
            self.tasks.discard(task)

    async def shutdown(self):
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def run(args):
    relay = Relay(args.pool_host, args.pool_port, tls=args.tls)
    server = await asyncio.start_server(relay.handle, args.listen_host, args.listen_port)
    LOG.warning("Experimental relay only; not for production. Listening on %s", server.sockets[0].getsockname())
    try:
        async with server:
            await server.serve_forever()
    finally:
        server.close()
        await server.wait_closed()
        await relay.shutdown()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pool-host", required=True)
    parser.add_argument("--pool-port", type=int, required=True, choices=range(1, 65536), metavar="PORT")
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", type=int, default=3333, choices=range(1, 65536), metavar="PORT")
    parser.add_argument("--tls", action="store_true", help="Verify TLS certificate on upstream connection")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
