import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

from proxy import Relay


class RelayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.servers = []
        self.writers = []

    async def asyncTearDown(self):
        for writer in self.writers:
            writer.close()
            await writer.wait_closed()
        for server in self.servers:
            server.close()
            await server.wait_closed()
        if hasattr(self, "relay"):
            await self.relay.shutdown()

    async def start_server(self, callback):
        server = await asyncio.start_server(callback, "127.0.0.1", 0)
        self.servers.append(server)
        return server.sockets[0].getsockname()[1]

    async def start_relay(self, port, limit=128):
        self.relay = Relay("127.0.0.1", port, limit=limit)
        return await self.start_server(self.relay.handle)

    async def connect(self, port):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        self.writers.append(writer)
        return reader, writer

    async def test_stratum_messages_forwarded_unchanged(self):
        received = []
        async def pool(reader, writer):
            for _ in range(3):
                received.append(await reader.readline())
            writer.write(b'{"id":1,"result":true,"error":null}\n'
                         b'{"id":null,"method":"mining.notify","params":["job"]}\n')
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        port = await self.start_relay(await self.start_server(pool))
        reader, writer = await self.connect(port)
        messages = [
            {"id": 1, "method": "mining.configure", "params": [["version-rolling"], {}]},
            {"id": 2, "method": "mining.authorize", "params": ["alice.worker1", "x"]},
            {"id": 3, "method": "mining.submit", "params": ["alice.worker1", "job", "00", "01", "02"]},
        ]
        encoded = [json.dumps(x).encode() + b"\n" for x in messages]
        writer.write(b"".join(encoded))
        await writer.drain()
        self.assertEqual(json.loads(await reader.readline())["result"], True)
        self.assertEqual(json.loads(await reader.readline())["method"], "mining.notify")
        self.assertEqual(received, encoded)
        self.assertEqual(await asyncio.wait_for(reader.read(), 2), b"")

    async def test_unavailable_pool_closes_miner_connection(self):
        proxy_port = await self.start_relay(1)
        open_client = asyncio.open_connection
        with patch("proxy.asyncio.open_connection", new=AsyncMock(side_effect=ConnectionRefusedError)):
            reader, writer = await open_client("127.0.0.1", proxy_port)
            self.writers.append(writer)
            self.assertEqual(await asyncio.wait_for(reader.read(), 2), b"")

    async def test_connection_limit_and_recovery(self):
        async def pool(reader, writer):
            await reader.read()
            writer.close()
            await writer.wait_closed()

        port = await self.start_relay(await self.start_server(pool), limit=1)
        _, first = await self.connect(port)
        await asyncio.sleep(0.05)
        rejected, _ = await self.connect(port)
        self.assertEqual(await asyncio.wait_for(rejected.read(), 2), b"")
        first.close()
        await first.wait_closed()
        for _ in range(100):
            if self.relay.active == 0:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(self.relay.active, 0)
        _, last = await self.connect(port)
        await asyncio.sleep(0.05)
        self.assertEqual(self.relay.active, 1)
        last.close()
