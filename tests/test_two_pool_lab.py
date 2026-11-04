"""Deterministic, synthetic two-pool experiment; no real DevFee or ASIC claims."""
import asyncio
import json
import time
import unittest

from lab.pool import NTIME, SHARE_TARGET, build_header, find_nonce
from lab.pow import meets_target
from proxy import Relay


def encode(value):
    return json.dumps(value, separators=(",", ":")).encode() + b"\n"


class SyntheticPool:
    def __init__(self, extranonce, job_id, worker):
        self.extranonce, self.job_id = extranonce, job_id
        self.worker = worker
        self.accepted = 0
        self.rejected = 0
        self.seen = set()
        self.writer = None

    async def handle(self, reader, writer):
        self.writer = writer
        authorized = False
        try:
            while line := await reader.readline():
                msg = json.loads(line)
                method, ident = msg.get("method"), msg.get("id")
                params = msg.get("params", [])
                if method == "mining.subscribe":
                    response = {"id": ident, "result": [[['mining.notify', 'lab']], self.extranonce, 4], "error": None}
                elif method == "mining.authorize":
                    authorized = params[0] == self.worker
                    response = {"id": ident, "result": authorized, "error": None}
                elif method == "mining.submit":
                    worker, job, extra, ntime, nonce = params
                    key = (extra, nonce)
                    valid = (authorized and worker == self.worker and job == self.job_id and
                             ntime == NTIME and key not in self.seen and
                             meets_target(build_header(self.extranonce, extra, nonce), SHARE_TARGET))
                    if valid:
                        self.seen.add(key)
                        self.accepted += 1
                    else:
                        self.rejected += 1
                    response = {"id": ident, "result": valid, "error": None if valid else [21, "stale", None]}
                else:
                    raise AssertionError("unexpected synthetic protocol request")
                writer.write(encode(response))
                if method == "mining.authorize":
                    writer.write(encode({"id": None, "method": "mining.notify", "params": [self.job_id]}))
                await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass


class TwoPoolExperiment(unittest.IsolatedAsyncioTestCase):
    async def test_reconnect_isolates_jobs_and_sha256d_share_counts(self):
        start = time.monotonic()
        first = SyntheticPool("11111111", "first-job", "alice.worker")
        second = SyntheticPool("22222222", "second-job", "devfee.worker")
        first_server = await asyncio.start_server(first.handle, "127.0.0.1", 0)
        second_server = await asyncio.start_server(second.handle, "127.0.0.1", 0)
        first_port = first_server.sockets[0].getsockname()[1]
        second_port = second_server.sockets[0].getsockname()[1]
        relay = Relay("127.0.0.1", first_port, backup=("127.0.0.1", second_port), timeout=1)
        proxy_server = await asyncio.start_server(relay.handle, "127.0.0.1", 0)
        proxy_port = proxy_server.sockets[0].getsockname()[1]
        writers = []

        async def connect(worker):
            reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
            writers.append(writer)
            async def request(obj):
                writer.write(encode(obj))
                await writer.drain()
                return json.loads(await asyncio.wait_for(reader.readline(), 5))
            subscribe = await request({"id": 1, "method": "mining.subscribe", "params": []})
            auth = await request({"id": 2, "method": "mining.authorize", "params": [worker, "x"]})
            self.assertTrue(auth["result"])
            job = json.loads(await asyncio.wait_for(reader.readline(), 5))["params"][0]
            return reader, writer, request, subscribe["result"][1], job

        async def submit(request, job, nonce, ident):
            return await request({"id": ident, "method": "mining.submit", "params":
                                  ["devfee.worker", job, "00000000", NTIME, nonce]})

        async def submit_batch(reader, writer, extranonce, job, start_id, count):
            payload = []
            next_nonce = 0
            last_nonce = None
            for ident in range(start_id, start_id + count):
                last_nonce = find_nonce(extranonce, start=next_nonce)
                next_nonce = int(last_nonce, 16) + 1
                payload.append(encode({"id": ident, "method": "mining.submit", "params":
                                       ["alice.worker", job, "00000000", NTIME, last_nonce]}))
            writer.writelines(payload)
            await writer.drain()
            for ident in range(start_id, start_id + count):
                reply = json.loads(await asyncio.wait_for(reader.readline(), 10))
                self.assertEqual(reply["id"], ident)
                self.assertTrue(reply["result"])
            return last_nonce

        try:
            reader_a, writer_a, _, extra_a, job_a = await connect("alice.worker")
            self.assertEqual((extra_a, job_a), ("11111111", "first-job"))
            await submit_batch(reader_a, writer_a, extra_a, job_a, 10, 990)
            self.assertEqual(first.accepted, 990)

            # Stop both primary listener and active primary session; the miner reconnects explicitly.
            first_server.close()
            await first_server.wait_closed()
            first.writer.close()
            await first.writer.wait_closed()
            self.assertEqual(await asyncio.wait_for(reader_a.read(), 5), b"")
            _, _, req_b, extra_b, job_b = await connect("devfee.worker")
            self.assertEqual((extra_b, job_b), ("22222222", "second-job"))
            self.assertTrue(any(s["pool"] == "backup" for s in relay.session_snapshot()))
            # Pick a share that was accepted by A but does not meet B's target
            # under B's extranonce. A random old nonce could happen to satisfy both.
            old_nonce = next(n for _, n in first.seen
                             if not meets_target(build_header(extra_b, "00000000", n), SHARE_TARGET))
            self.assertTrue(meets_target(build_header(extra_a, "00000000", old_nonce), SHARE_TARGET))
            stale = await submit(req_b, job_a, old_nonce, 1001)
            self.assertFalse(stale["result"])
            invalid_pow = await submit(req_b, job_b, old_nonce, 1002)
            self.assertFalse(invalid_pow["result"])
            next_nonce = 0
            for ident in range(1010, 1020):
                nonce = find_nonce(extra_b, start=next_nonce)
                next_nonce = int(nonce, 16) + 1
                self.assertTrue((await submit(req_b, job_b, nonce, ident))["result"])
            self.assertEqual((first.accepted, first.rejected), (990, 0))
            self.assertEqual((second.accepted, second.rejected), (10, 2))
            self.assertEqual((relay.metrics.submitted, relay.metrics.accepted, relay.metrics.rejected), (1002, 1000, 2))
            self.assertEqual(relay.metrics.backup_connections, 1)
            print(f"\nSynthetic two-pool run: 990 + 10 accepted, 2 invalid rejected; {time.monotonic() - start:.2f}s")
        finally:
            for writer in writers:
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass
            proxy_server.close()
            first_server.close()
            second_server.close()
            await proxy_server.wait_closed()
            await first_server.wait_closed()
            await second_server.wait_closed()
            await relay.shutdown()
