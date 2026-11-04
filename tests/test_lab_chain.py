import asyncio
import json
import unittest

from lab.pool import LabPool, find_nonce, build_header, SHARE_TARGET, NTIME
from lab.pow import meets_target
from proxy import Relay


class LabChainTests(unittest.IsolatedAsyncioTestCase):
    async def test_backup_pool_receives_original_authorization(self):
        pool = LabPool()
        pool_server = await asyncio.start_server(pool.handle, "127.0.0.1", 0)
        backup = ("127.0.0.1", pool_server.sockets[0].getsockname()[1])
        relay = Relay("127.0.0.1", 1, backup=backup, timeout=1)
        proxy_server = await asyncio.start_server(relay.handle, "127.0.0.1", 0)
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy_server.sockets[0].getsockname()[1])

        async def request(value):
            writer.write(json.dumps(value).encode() + b"\n")
            await writer.drain()
            return json.loads(await asyncio.wait_for(reader.readline(), 3))

        try:
            subscribe = await request({"id": 1, "method": "mining.subscribe", "params": []})
            extranonce = subscribe["result"][1]
            auth = await request({"id": 2, "method": "mining.authorize", "params": ["alice.worker", "x"]})
            self.assertTrue(auth["result"])
            self.assertEqual(relay.session_snapshot()[0]["pool"], "backup")
            self.assertEqual(relay.session_snapshot()[0]["worker"], "alice.worker")
            await reader.readline()  # difficulty
            job = json.loads(await reader.readline())
            nonce = find_nonce(extranonce)
            share = await request({"id": 3, "method": "mining.submit", "params":
                                   ["alice.worker", job["params"][0], "00000000", NTIME, nonce]})
            self.assertTrue(share["result"])
            self.assertEqual(pool.accepted, 1)
            self.assertEqual(relay.metrics.backup_connections, 1)
        finally:
            writer.close()
            await writer.wait_closed()
            proxy_server.close()
            pool_server.close()
            await proxy_server.wait_closed()
            await pool_server.wait_closed()
            await relay.shutdown()

    async def test_real_sha256d_share_through_proxy(self):
        pool = LabPool()
        pool_server = await asyncio.start_server(pool.handle, "127.0.0.1", 0)
        pool_port = pool_server.sockets[0].getsockname()[1]
        relay = Relay("127.0.0.1", pool_port)
        proxy_server = await asyncio.start_server(relay.handle, "127.0.0.1", 0)
        proxy_port = proxy_server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)

        async def request(value):
            writer.write(json.dumps(value).encode() + b"\n")
            await writer.drain()
            return json.loads(await asyncio.wait_for(reader.readline(), 3))

        try:
            response = await request({"id": 1, "method": "mining.subscribe", "params": []})
            extranonce1 = response["result"][1]
            response = await request({"id": 2, "method": "mining.authorize", "params": ["alice.worker", "x"]})
            self.assertTrue(response["result"])
            self.assertEqual(relay.session_snapshot()[0]["worker"], "alice.worker")
            difficulty = json.loads(await reader.readline())
            self.assertEqual(difficulty["method"], "mining.set_difficulty")
            job = json.loads(await reader.readline())
            self.assertEqual(job["method"], "mining.notify")
            nonce = find_nonce(extranonce1)
            params = ["alice.worker", job["params"][0], "00000000", NTIME, nonce]
            response = await request({"id": 3, "method": "mining.submit", "params": params})
            self.assertTrue(response["result"])
            response = await request({"id": 4, "method": "mining.submit", "params": params})
            self.assertFalse(response["result"])
            invalid_nonce = next(f"{i:08x}" for i in range(1000)
                                 if not meets_target(build_header(extranonce1, "00000000", f"{i:08x}"), SHARE_TARGET))
            invalid = ["alice.worker", job["params"][0], "00000000", NTIME, invalid_nonce]
            response = await request({"id": 5, "method": "mining.submit", "params": invalid})
            self.assertFalse(response["result"])
            wrong_worker = ["bob.worker", *params[1:]]
            response = await request({"id": 6, "method": "mining.submit", "params": wrong_worker})
            self.assertFalse(response["result"])
            self.assertEqual((pool.accepted, pool.rejected), (1, 3))
            self.assertEqual((relay.metrics.submitted, relay.metrics.accepted, relay.metrics.rejected), (4, 1, 3))
            self.assertEqual(relay.metrics.share_rtt_samples, 4)
            self.assertGreaterEqual(relay.metrics.share_rtt_total_ms, 0)
            current = relay.session_snapshot()[0]
            self.assertEqual((current["submitted"], current["accepted"], current["rejected"]), (4, 1, 3))
        finally:
            writer.close()
            await writer.wait_closed()
            proxy_server.close()
            pool_server.close()
            await proxy_server.wait_closed()
            await pool_server.wait_closed()
            await relay.shutdown()
