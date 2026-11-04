"""Synthetic protocol vectors: byte preservation, not hardware certification."""
import asyncio
import unittest

from proxy import Relay


class ProtocolTransparencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_fragmented_extensions_and_opaque_fields_are_preserved(self):
        # Synthetic vectors deliberately contain unknown extensions and noncanonical spacing.
        downstream = (
            b'{"id":1,"method":"mining.configure","params":[["version-rolling","future-ext"],'
            b'{"version-rolling.mask":"1fffe000","future-ext.value":"opaque"}]}\n'
            b'{"id":2,"method":"mining.subscribe","params":["synthetic-miner/1.0"]}\n'
            b'{"id":3,"method":"mining.authorize","params":["acct.worker","secret"]}\n'
            b'{"id":4,"method":"mining.submit","params":["acct.worker","job",'
            b'"00000000","65000000","00000001","00002000"],"custom": 7}\n'
        )
        upstream = (
            b'{"id":1,"result":{"version-rolling":true,"version-rolling.mask":"1fffe000"},"error":null}\n'
            b'{"id":2,"result":[[["mining.notify","1"]],"12345678",4],"error":null}\n'
            b'{"id":3,"result":true,"error":null}\n'
            b'{"id":null,"method":"mining.set_extranonce","params":["87654321",4]}\n'
            b'{"id":null,"method":"mining.set_version_mask","params":["1fffe000"]}\n'
            b'{"id":null,"method":"client.reconnect","params":["backup.example",3333,0]}\n'
            b'{"id":null,"method":"mining.notify","params":["job","opaque"],"extra":true}\n'
            b'{"id":4,"result":false,"error":[21,"stale",null]}\n'
        )
        received = bytearray()
        async def pool(reader, writer):
            try:
                while len(received) < len(downstream):
                    data = await reader.read(len(downstream) - len(received))
                    if not data:
                        break
                    received.extend(data)
                for start in range(0, len(upstream), 13):
                    writer.write(upstream[start:start + 13])
                    await writer.drain()
                await reader.read()
            finally:
                writer.close()
                await writer.wait_closed()

        pool_server = await asyncio.start_server(pool, "127.0.0.1", 0)
        relay = Relay("127.0.0.1", pool_server.sockets[0].getsockname()[1])
        proxy_server = await asyncio.start_server(relay.handle, "127.0.0.1", 0)
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy_server.sockets[0].getsockname()[1])
        try:
            for start in range(0, len(downstream), 7):
                writer.write(downstream[start:start + 7])
                await writer.drain()
            result = await asyncio.wait_for(reader.readexactly(len(upstream)), 5)
            self.assertEqual(bytes(received), downstream)
            self.assertEqual(result, upstream)
            self.assertEqual(relay.metrics.submitted, 1)
            self.assertEqual(relay.metrics.rejected, 1)
            self.assertEqual(relay.metrics.redirect_notices, 1)
        finally:
            writer.close()
            await writer.wait_closed()
            proxy_server.close()
            pool_server.close()
            await proxy_server.wait_closed()
            await pool_server.wait_closed()
            await relay.shutdown()
