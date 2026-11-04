import asyncio
import unittest

from proxy import Relay


class IdleTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.servers = []
        self.writers = []
        self.relay = None

    async def asyncTearDown(self):
        for writer in self.writers:
            writer.close()
            await writer.wait_closed()
        for server in self.servers:
            server.close()
            await server.wait_closed()
        if self.relay:
            await self.relay.shutdown()

    async def start(self, callback, idle_timeout):
        pool = await asyncio.start_server(callback, "127.0.0.1", 0)
        self.servers.append(pool)
        self.relay = Relay("127.0.0.1", pool.sockets[0].getsockname()[1], idle_timeout=idle_timeout)
        server = await asyncio.start_server(self.relay.handle, "127.0.0.1", 0)
        self.servers.append(server)
        reader, writer = await asyncio.open_connection("127.0.0.1", server.sockets[0].getsockname()[1])
        self.writers.append(writer)
        return reader, writer

    async def test_idle_session_releases_capacity(self):
        async def pool(reader, writer):
            await reader.read()
            writer.close()
            await writer.wait_closed()
        reader, _ = await self.start(pool, .2)
        self.assertEqual(await asyncio.wait_for(reader.read(), 3), b"")
        self.assertEqual(self.relay.metrics.idle_timeouts, 1)
        for _ in range(100):
            if self.relay.active == 0:
                break
            await asyncio.sleep(.01)
        self.assertEqual(self.relay.active, 0)

    async def test_pool_traffic_keeps_session_alive(self):
        async def pool(reader, writer):
            try:
                for _ in range(6):
                    writer.write(b"job\n")
                    await writer.drain()
                    await asyncio.sleep(.1)
                await reader.read()
            finally:
                writer.close()
                await writer.wait_closed()
        reader, _ = await self.start(pool, .5)
        for _ in range(6):
            self.assertEqual(await asyncio.wait_for(reader.readline(), 2), b"job\n")
        self.assertEqual(self.relay.metrics.idle_timeouts, 0)
        self.assertEqual(await asyncio.wait_for(reader.read(), 3), b"")
        self.assertEqual(self.relay.metrics.idle_timeouts, 1)
