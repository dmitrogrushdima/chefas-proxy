import asyncio
import json
import unittest
import socket
from unittest.mock import AsyncMock, patch

from proxy import Relay, is_loopback_bind


class RelayTests(unittest.IsolatedAsyncioTestCase):
    def test_loopback_bind_detection_is_conservative(self):
        self.assertTrue(is_loopback_bind("127.0.0.1"))
        self.assertTrue(is_loopback_bind("::1"))
        self.assertTrue(is_loopback_bind("localhost"))
        self.assertFalse(is_loopback_bind("0.0.0.0"))
        self.assertFalse(is_loopback_bind("unresolved.example"))

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
        server = await asyncio.start_server(callback, "127.0.0.1", 0, backlog=512)
        self.servers.append(server)
        return server.sockets[0].getsockname()[1]

    async def start_relay(self, port, limit=128):
        self.relay = Relay("127.0.0.1", port, limit=limit)
        return await self.start_server(self.relay.handle)

    async def connect(self, port):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        self.writers.append(writer)
        return reader, writer

    async def test_tp_mode_bypasses_stratum_observer(self):
        payload = b'\xff\x00HTTP arbitrary binary\r\n'
        async def pool(reader, writer):
            data = await reader.readexactly(len(payload))
            writer.write(data)
            await writer.drain()
            writer.close()
            await writer.wait_closed()
        port = await self.start_relay(await self.start_server(pool))
        self.relay.active_router = True
        self.relay.tp_mode = True
        with patch('observer.Observer.feed') as observe:
            reader, writer = await self.connect(port)
            writer.write(payload)
            await writer.drain()
            self.assertEqual(await asyncio.wait_for(reader.readexactly(len(payload)), 3), payload)
            observe.assert_not_called()
        self.assertEqual(self.relay.metrics.submitted, 0)

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
        self.assertEqual(self.relay.metrics.submitted, 1)
        self.assertEqual(self.relay.metrics.accepted, 0)

    async def test_unavailable_pool_closes_miner_connection(self):
        proxy_port = await self.start_relay(1)
        open_client = asyncio.open_connection
        with patch("upstream_manager.open_upstream", new=AsyncMock(side_effect=ConnectionRefusedError)):
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
        self.assertEqual(self.relay.metrics.refused_capacity, 1)
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

    async def test_unlisted_network_is_rejected_before_pool_connection(self):
        connected = asyncio.Event()
        async def pool(reader, writer):
            connected.set()
            writer.close()
            await writer.wait_closed()
        pool_port = await self.start_server(pool)
        self.relay = Relay("127.0.0.1", pool_port, allowed_networks=("192.0.2.0/24",))
        port = await self.start_server(self.relay.handle)
        reader, _ = await self.connect(port)
        self.assertEqual(await asyncio.wait_for(reader.read(), 2), b"")
        self.assertFalse(connected.is_set())
        self.assertEqual(self.relay.metrics.refused_network, 1)
        self.assertEqual(self.relay.metrics.connections, 0)

    async def test_listed_network_connects_normally(self):
        async def pool(reader, writer):
            writer.write(await reader.readline())
            await writer.drain()
            writer.close()
            await writer.wait_closed()
        pool_port = await self.start_server(pool)
        self.relay = Relay("127.0.0.1", pool_port, allowed_networks=("192.0.2.0/24", "127.0.0.0/8"))
        port = await self.start_server(self.relay.handle)
        reader, writer = await self.connect(port)
        writer.write(b"allowed\n")
        await writer.drain()
        self.assertEqual(await asyncio.wait_for(reader.readline(), 2), b"allowed\n")
        self.assertEqual(self.relay.metrics.refused_network, 0)

    async def test_network_filter_handles_ipv6_and_unavailable_peer(self):
        relay = Relay("localhost", 3333, allowed_networks=("192.0.2.0/24", "2001:db8::/32"))
        self.assertTrue(relay.peer_allowed(("::ffff:192.0.2.1", 123)))
        self.assertTrue(relay.peer_allowed(("2001:db8::1", 123, 0, 0)))
        self.assertFalse(relay.peer_allowed(("2001:db9::1", 123, 0, 0)))
        self.assertFalse(relay.peer_allowed(None))

    async def test_backup_on_initial_connect_failure(self):
        async def pool(reader, writer):
            writer.write(await reader.read(100))
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        backup_port = await self.start_server(pool)
        self.relay = Relay("127.0.0.1", 1, backup=("127.0.0.1", backup_port), timeout=1)
        port = await self.start_server(self.relay.handle)
        reader, writer = await self.connect(port)
        writer.write(b"worker\n")
        await writer.drain()
        self.assertEqual(await asyncio.wait_for(reader.read(), 3), b"worker\n")
        self.assertEqual(self.relay.metrics.upstream_failures, 1)
        self.assertEqual(self.relay.metrics.backup_connections, 1)

    async def test_disconnect_then_reconnect_uses_backup(self):
        first_connected = asyncio.Event()
        close_first = asyncio.Event()

        async def primary(reader, writer):
            first_connected.set()
            await close_first.wait()
            writer.close()
            await writer.wait_closed()

        async def backup(reader, writer):
            writer.write(await reader.readline())
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        primary_port = await self.start_server(primary)
        primary_server = self.servers[-1]
        backup_port = await self.start_server(backup)
        self.relay = Relay("127.0.0.1", primary_port, backup=("127.0.0.1", backup_port), timeout=1)
        port = await self.start_server(self.relay.handle)
        reader, _ = await self.connect(port)
        await asyncio.wait_for(first_connected.wait(), 2)
        primary_server.close()
        await primary_server.wait_closed()
        close_first.set()
        self.assertEqual(await asyncio.wait_for(reader.read(), 2), b"")
        reader2, writer2 = await self.connect(port)
        writer2.write(b"reconnected\n")
        await writer2.drain()
        self.assertEqual(await asyncio.wait_for(reader2.readline(), 3), b"reconnected\n")
        self.assertEqual(self.relay.metrics.upstream_failures, 1)
        self.assertEqual(self.relay.metrics.backup_connections, 1)

    async def test_shutdown_closes_sessions_and_releases_counters(self):
        async def pool(reader, writer):
            await reader.read()
            writer.close()
            await writer.wait_closed()
        port = await self.start_relay(await self.start_server(pool))
        reader, _ = await self.connect(port)
        for _ in range(100):
            if self.relay.session_snapshot() and self.relay.session_snapshot()[0]["state"] == "connected":
                break
            await asyncio.sleep(0.01)
        self.assertEqual(self.relay.active, 1)
        await self.relay.shutdown()
        self.assertEqual(await asyncio.wait_for(reader.read(), 2), b"")
        self.assertEqual(self.relay.active, 0)
        self.assertEqual(self.relay.metrics.active, 0)
        self.assertEqual(self.relay.session_snapshot(), [])

    async def test_own_listener_as_pool_is_blocked_without_spawn_loop(self):
        self.relay = Relay("localhost", 1, limit=10, timeout=1)
        server = await asyncio.start_server(self.relay.handle, "127.0.0.1", 0)
        self.servers.append(server)
        self.relay.port = server.sockets[0].getsockname()[1]
        self.relay.set_listeners(server)
        reader, _ = await self.connect(self.relay.port)
        self.assertEqual(await asyncio.wait_for(reader.read(), 3), b"")
        self.assertEqual(self.relay.metrics.blocked_loops, 1)
        self.assertEqual(self.relay.metrics.connections, 1)

    async def test_wildcard_listener_blocks_local_pool_address(self):
        self.relay = Relay("127.0.0.1", 1, limit=10, timeout=1)
        server = await asyncio.start_server(self.relay.handle, "0.0.0.0", 0)
        self.servers.append(server)
        self.relay.port = server.sockets[0].getsockname()[1]
        self.relay.set_listeners(server)
        reader, _ = await self.connect(self.relay.port)
        self.assertEqual(await asyncio.wait_for(reader.read(), 3), b"")
        self.assertEqual(self.relay.metrics.blocked_loops, 1)

    async def test_same_port_on_external_address_is_not_identified_as_loop(self):
        self.relay = Relay("external.test", 1, timeout=1)
        server = await asyncio.start_server(self.relay.handle, "0.0.0.0", 0)
        self.servers.append(server)
        port = server.sockets[0].getsockname()[1]
        self.relay.set_listeners(server)
        answer = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", port))]
        with patch.object(self.relay.resolver, "resolve", new=AsyncMock(return_value=answer)):
            self.assertFalse(await self.relay.is_self_route(("external.test", port)))

    async def test_many_clients_keep_traffic_separate(self):
        async def pool(reader, writer):
            line = await reader.readline()
            writer.write(line)
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        port = await self.start_relay(await self.start_server(pool), limit=512)
        clients = []
        for i in range(400):
            clients.append(await self.connect(port))
        async def client(i, reader, writer):
            data = ("worker-%s\n" % i).encode()
            writer.write(data)
            await writer.drain()
            return await asyncio.wait_for(reader.readline(), 20) == data
        self.assertTrue(all(await asyncio.gather(*(client(i, *pair) for i, pair in enumerate(clients)))))
