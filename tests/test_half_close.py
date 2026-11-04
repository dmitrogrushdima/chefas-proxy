"""TCP half-close test using a synthetic request/response pool."""
import asyncio
import unittest

from proxy import Relay


class HalfCloseTests(unittest.IsolatedAsyncioTestCase):
    async def test_client_half_close_still_receives_pool_response(self):
        received = []
        pool_finished = asyncio.Event()
        request = b'{"id":1,"method":"mining.submit","params":[]}\n'
        response = b'{"id":1,"result":true,"error":null}\n'

        async def pool(reader, writer):
            try:
                # A half-close signals the end of request bytes, not the end of replies.
                received.append(await reader.read())
                writer.write(response)
                await writer.drain()
            except OSError:
                pass
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass
                pool_finished.set()

        pool_server = await asyncio.start_server(pool, "127.0.0.1", 0)
        relay = Relay("127.0.0.1", pool_server.sockets[0].getsockname()[1])
        proxy_server = await asyncio.start_server(relay.handle, "127.0.0.1", 0)
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy_server.sockets[0].getsockname()[1])
        try:
            self.assertTrue(writer.can_write_eof(), "TCP transport must support half-close")
            writer.write(request)
            await writer.drain()
            writer.write_eof()
            reply = await asyncio.wait_for(reader.read(), 3)
            await asyncio.wait_for(pool_finished.wait(), 3)
            self.assertEqual(received, [request])
            self.assertEqual(reply, response)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
            proxy_server.close()
            pool_server.close()
            await proxy_server.wait_closed()
            await pool_server.wait_closed()
            await relay.shutdown()
