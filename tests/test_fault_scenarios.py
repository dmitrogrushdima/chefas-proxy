"""Synthetic Stratum fault tests; these do not certify actual pools or ASICs."""
import asyncio
import json
import unittest

from proxy import Relay


def encode(message):
    return json.dumps(message, separators=(",", ":")).encode() + b"\n"


class FaultScenarioTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.servers = []
        self.writers = []
        self.relay = None

    async def server(self, handler):
        instance = await asyncio.start_server(handler, "127.0.0.1", 0)
        self.servers.append(instance)
        return instance.sockets[0].getsockname()[1]

    async def open_client(self, port):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        self.writers.append(writer)
        return reader, writer

    async def asyncTearDown(self):
        for writer in self.writers:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
        for server in self.servers:
            server.close()
            await server.wait_closed()
        if self.relay:
            await self.relay.shutdown()

    async def test_disconnect_during_submit_does_not_replay_to_backup(self):
        seen_primary = []
        backup_contacted = asyncio.Event()

        async def primary(reader, writer):
            try:
                seen_primary.append(await reader.readline())
                # Drop the connection without acknowledging the submitted share.
            finally:
                writer.close()
                await writer.wait_closed()

        async def backup(reader, writer):
            backup_contacted.set()
            writer.close()
            await writer.wait_closed()

        primary_port = await self.server(primary)
        backup_port = await self.server(backup)
        self.relay = Relay("127.0.0.1", primary_port, backup=("127.0.0.1", backup_port))
        proxy_port = await self.server(self.relay.handle)
        reader, writer = await self.open_client(proxy_port)
        share = encode({"id": 5, "method": "mining.submit", "params": ["alice.worker", "job", "00", "01", "02"]})
        writer.write(share)
        await writer.drain()
        self.assertEqual(await asyncio.wait_for(reader.read(), 3), b"")
        # Session closure records unresolved shares; it must not fabricate a success.
        for _ in range(100):
            if self.relay.active == 0:
                break
            await asyncio.sleep(.01)
        self.assertEqual(seen_primary, [share])
        self.assertFalse(backup_contacted.is_set())
        self.assertEqual((self.relay.metrics.submitted, self.relay.metrics.accepted,
                          self.relay.metrics.rejected, self.relay.metrics.unmatched), (1, 0, 0, 1))

    async def test_same_request_id_is_isolated_between_simultaneous_miners(self):
        async def pool(reader, writer):
            try:
                request = json.loads(await reader.readline())
                accepted = request["params"][0] == "alice.worker"
                writer.write(encode({"id": request["id"], "result": accepted,
                                     "error": None if accepted else [23, "invalid", None]}))
                await writer.drain()
                await reader.read()
            finally:
                writer.close()
                await writer.wait_closed()

        pool_port = await self.server(pool)
        self.relay = Relay("127.0.0.1", pool_port)
        proxy_port = await self.server(self.relay.handle)
        alice_reader, alice_writer = await self.open_client(proxy_port)
        bob_reader, bob_writer = await self.open_client(proxy_port)
        for writer, name in ((alice_writer, "alice.worker"), (bob_writer, "bob.worker")):
            writer.write(encode({"id": 42, "method": "mining.submit", "params": [name, "job", "00", "01", "02"]}))
            await writer.drain()
        alice, bob = await asyncio.gather(asyncio.wait_for(alice_reader.readline(), 3),
                                          asyncio.wait_for(bob_reader.readline(), 3))
        self.assertTrue(json.loads(alice)["result"])
        self.assertFalse(json.loads(bob)["result"])
        sessions = self.relay.session_snapshot()
        self.assertCountEqual([(x["submitted"], x["accepted"], x["rejected"]) for x in sessions],
                              [(1, 1, 0), (1, 0, 1)])
        self.assertEqual((self.relay.metrics.submitted, self.relay.metrics.accepted,
                          self.relay.metrics.rejected, self.relay.metrics.unmatched), (2, 1, 1, 0))
