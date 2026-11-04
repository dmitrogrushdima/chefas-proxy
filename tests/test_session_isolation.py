import asyncio
import unittest
from unittest.mock import patch

from proxy import Relay


class SessionIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_pool_session_does_not_block_another(self):
        waiting = asyncio.Event()
        release = asyncio.Event()

        async def pool(reader, writer):
            try:
                request = await reader.readline()
                if request == b"slow\n":
                    waiting.set()
                    await release.wait()
                writer.write(request)
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        pool_server = await asyncio.start_server(pool, "127.0.0.1", 0)
        relay = Relay("127.0.0.1", pool_server.sockets[0].getsockname()[1])
        proxy_server = await asyncio.start_server(relay.handle, "127.0.0.1", 0)
        port = proxy_server.sockets[0].getsockname()[1]
        readers_writers = []
        try:
            slow_reader, slow_writer = await asyncio.open_connection("127.0.0.1", port)
            readers_writers.append(slow_writer)
            slow_writer.write(b"slow\n")
            await slow_writer.drain()
            await asyncio.wait_for(waiting.wait(), 3)
            fast_reader, fast_writer = await asyncio.open_connection("127.0.0.1", port)
            readers_writers.append(fast_writer)
            fast_writer.write(b"fast\n")
            await fast_writer.drain()
            self.assertEqual(await asyncio.wait_for(fast_reader.readline(), 3), b"fast\n")
            release.set()
            self.assertEqual(await asyncio.wait_for(slow_reader.readline(), 3), b"slow\n")
        finally:
            release.set()
            for writer in readers_writers:
                writer.close()
                await writer.wait_closed()
            proxy_server.close()
            pool_server.close()
            await proxy_server.wait_closed()
            await pool_server.wait_closed()
            await relay.shutdown()

    async def test_observer_exception_does_not_drop_stream(self):
        async def pool(reader, writer):
            writer.write(await reader.readline())
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        pool_server = await asyncio.start_server(pool, "127.0.0.1", 0)
        relay = Relay("127.0.0.1", pool_server.sockets[0].getsockname()[1])
        proxy_server = await asyncio.start_server(relay.handle, "127.0.0.1", 0)
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy_server.sockets[0].getsockname()[1])
        try:
            with patch("observer.Observer.feed", side_effect=ValueError("invalid observation")):
                writer.write(b"not-json\n")
                await writer.drain()
                self.assertEqual(await asyncio.wait_for(reader.readline(), 3), b"not-json\n")
            self.assertGreaterEqual(relay.metrics.observation_errors, 1)
        finally:
            writer.close()
            await writer.wait_closed()
            proxy_server.close()
            pool_server.close()
            await proxy_server.wait_closed()
            await pool_server.wait_closed()
            await relay.shutdown()
