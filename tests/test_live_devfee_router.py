"""PoW-checked integration tests through the actual proxy.py entrypoint.

The pools are deliberately local and synthetic. They validate each submitted
80-byte Bitcoin header independently of the router's own PoW validator.
"""
import asyncio
import json
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from lab.pool import (COINB1, COINB2, NBITS, NTIME, PREVHASH, SHARE_TARGET,
                      VERSION, build_header, find_nonce)
from lab.pow import compact_target, meets_target


ROOT = Path(__file__).resolve().parent.parent
DIFFICULTY = compact_target(0x1D00FFFF) / SHARE_TARGET


def encode(value):
    return json.dumps(value, separators=(",", ":")).encode() + b"\n"


def checked_header(extra, ex2, nonce, version_bits=None):
    """Independent fixture construction, including the negotiated BIP310 mask."""
    header = build_header(extra, ex2, nonce)
    if version_bits is None:
        return header
    bits = int(version_bits, 16)
    mask = 0x1FFFE000
    if bits & ~mask:
        raise ValueError("Version bits outside negotiated mask")
    version = (int(VERSION, 16) & ~mask) | bits
    return version.to_bytes(4, "little") + header[4:]


def unused_ports(count):
    sockets = []
    try:
        for _ in range(count):
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            sockets.append(sock)
        return [sock.getsockname()[1] for sock in sockets]
    finally:
        for sock in sockets:
            sock.close()


class CheckedPool:
    """Separate TCP sessions, extranonces and duplicate sets for each pool."""

    def __init__(self, extranonce, worker):
        self.extranonce = int(extranonce, 16)
        self.worker = worker
        self.accepted = 0
        self.rejected = 0
        self.connections = 0
        self.validated_extranonces = set()
        self.writers = set()
        self.errors = []
        self.configure_requests = []
        self.reject_submits = False

    async def start(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def close(self):
        self.server.close()
        await self.server.wait_closed()
        writers = list(self.writers)
        for writer in writers:
            writer.close()
        for writer in writers:
            try:
                await writer.wait_closed()
            except OSError:
                pass

    async def handle(self, reader, writer):
        extra = f"{self.extranonce + self.connections:08x}"
        self.connections += 1
        self.writers.add(writer)
        authorized = set()
        seen = set()
        subscribed = False
        try:
            while line := await reader.readline():
                req = json.loads(line)
                ident, method = req.get("id"), req.get("method")
                params = req.get("params", [])
                response = {"id": ident, "result": None, "error": None}
                if method == "mining.configure":
                    self.configure_requests.append(params)
                    requested = params[0] if params else []
                    response["result"] = {name: name == "version-rolling" for name in requested}
                    if "version-rolling" in requested:
                        response["result"]["version-rolling.mask"] = "1fffe000"
                elif method == "mining.subscribe":
                    subscribed = True
                    response["result"] = [[["mining.notify", "session"]], extra, 4]
                elif method == "mining.extranonce.subscribe":
                    response["result"] = True
                elif method == "mining.authorize":
                    response["result"] = subscribed and params[0] == self.worker
                    if response["result"]:
                        authorized.add(params[0])
                elif method == "mining.submit":
                    # Both pools deliberately use the SAME original job id.
                    # Only routing and the original session extranonce differ.
                    worker, job, ex2, ntime, nonce = params[:5]
                    key = (job, ex2, ntime, nonce, *params[5:])
                    valid = (not self.reject_submits and worker in authorized and job == "shared-job" and
                             ntime == NTIME and key not in seen and
                             meets_target(checked_header(extra, ex2, nonce, params[5] if len(params) > 5 else None), SHARE_TARGET))
                    response["result"] = valid
                    if valid:
                        seen.add(key)
                        self.accepted += 1
                        self.validated_extranonces.add(extra)
                    else:
                        self.rejected += 1
                        response["error"] = [23, "Invalid PoW or session", None]
                else:
                    response["error"] = [20, "Unsupported request", None]
                writer.write(encode(response))
                if method == "mining.authorize" and response["result"]:
                    writer.write(encode({"id": None, "method": "mining.set_difficulty", "params": [DIFFICULTY]}))
                    writer.write(encode({"id": None, "method": "mining.notify", "params": [
                        "shared-job", PREVHASH, COINB1, COINB2, [], VERSION, NBITS, NTIME, True]}))
                await writer.drain()
        except (OSError, asyncio.CancelledError):
            pass
        except Exception as exc:
            self.errors.append(repr(exc))
        finally:
            self.writers.discard(writer)
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass


class Miner:
    def __init__(self, port, *, hot=True, configure=False):
        self.port = port
        self.hot = hot
        self.next_id = 0
        self.pending = {}
        self.notifications = []
        self.extra = None
        self.difficulty = None
        self.job = None
        self.job_event = asyncio.Event()
        self.reconnect = asyncio.Event()
        self.closed = asyncio.Event()
        self.nonces = {}
        self.configure = configure
        self.version_bits = None

    async def connect(self):
        self.reader, self.writer = await asyncio.open_connection("127.0.0.1", self.port)
        self.reader_task = asyncio.create_task(self.read_loop())
        if self.configure:
            configured = await self.request("mining.configure", [["version-rolling"], {
                "version-rolling.mask": "1fffe000", "version-rolling.min-bit-count": 2}])
            if configured.get("result", {}).get("version-rolling.mask") != "1fffe000":
                raise AssertionError(configured)
            self.version_bits = "00002000"
        subscribed = await self.request("mining.subscribe", ["kontur-integration/1"])
        if subscribed.get("error"):
            raise AssertionError(subscribed)
        self.extra = subscribed["result"][1]
        if self.hot:
            extension = await self.request("mining.extranonce.subscribe", [])
            if extension.get("result") is not True:
                raise AssertionError(extension)
        authorized = await self.request("mining.authorize", ["user.worker", "x"])
        if authorized.get("result") is not True:
            raise AssertionError(authorized)
        await asyncio.wait_for(self.job_event.wait(), 5)
        return self

    async def read_loop(self):
        try:
            while line := await self.reader.readline():
                msg = json.loads(line)
                method = msg.get("method")
                if method:
                    self.notifications.append(msg)
                    params = msg.get("params", [])
                    if method == "mining.set_extranonce":
                        self.extra = params[0]
                    elif method == "mining.set_difficulty":
                        self.difficulty = params[0]
                    elif method == "mining.notify":
                        self.job = {"params": params, "extra": self.extra, "diff": self.difficulty}
                        self.job_event.set()
                    elif method == "client.reconnect":
                        self.reconnect.set()
                else:
                    future = self.pending.pop(msg.get("id"), None)
                    if future is not None and not future.done():
                        future.set_result(msg)
        except OSError:
            pass
        finally:
            self.closed.set()
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(ConnectionError("ASIC socket closed"))
            self.pending.clear()

    async def request(self, method, params):
        self.next_id += 1
        future = asyncio.get_running_loop().create_future()
        self.pending[self.next_id] = future
        self.writer.write(encode({"id": self.next_id, "method": method, "params": params}))
        await self.writer.drain()
        return await asyncio.wait_for(future, 5)

    def share_params(self, job=None):
        job = job or self.job
        extra = job["extra"]
        start = self.nonces.get(extra, 0)
        if self.version_bits is None:
            nonce = find_nonce(extra, start=start)
        else:
            nonce = next((f"{number:08x}" for number in range(start, start + 100000)
                          if meets_target(checked_header(extra, "00000000", f"{number:08x}", self.version_bits), SHARE_TARGET)), None)
            if nonce is None:
                raise AssertionError("No test share found")
        self.nonces[extra] = int(nonce, 16) + 1
        params = ["user.worker", job["params"][0], "00000000", NTIME, nonce]
        if self.version_bits is not None:
            params.append(self.version_bits)
        return params

    async def submit(self, params=None):
        response = await self.request("mining.submit", params or self.share_params())
        # Let the reader consume a switch notification sent after the RPC reply.
        await asyncio.sleep(.005)
        return response

    async def close(self):
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except OSError:
            pass
        self.reader_task.cancel()
        await asyncio.gather(self.reader_task, return_exceptions=True)


class LiveDevFeeRouterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pools = []
        self.miners = []
        self.process = None
        self.stderr = tempfile.TemporaryFile()

    async def asyncTearDown(self):
        for miner in self.miners:
            await miner.close()
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(asyncio.to_thread(self.process.wait), 5)
            except asyncio.TimeoutError:
                self.process.kill()
                await asyncio.to_thread(self.process.wait)
        for pool in self.pools:
            await pool.close()
            self.assertEqual(pool.errors, [], "Pool fixture failed")
        self.stderr.close()

    async def pool(self, extra, worker):
        pool = await CheckedPool(extra, worker).start()
        self.pools.append(pool)
        return pool

    async def launch(self, *, fee=True, backup=None, require_fee=False):
        self.user_pool = await self.pool("11111111", "user.worker")
        if fee:
            self.fee_pool = await self.pool("22222222", "fee.worker")
        self.port, self.dashboard = unused_ports(2)
        args = [sys.executable, str(ROOT / "proxy.py"), "--active-router", "--validate-pow",
                "--pool-host", "127.0.0.1", "--pool-port", str(self.user_pool.port),
                "--listen-port", str(self.port), "--dashboard-port", str(self.dashboard),
                "--connect-timeout", "1", "--reconnect-min-seconds", "0"]
        if fee:
            args += ["--devfee-host", "127.0.0.1", "--devfee-port", str(self.fee_pool.port),
                     "--devfee-worker", "fee.worker", "--fee-min-work", "0"]
        if require_fee:
            args += ["--require-devfee", "--fee-grace-seconds", "0.5"]
        if backup:
            args += ["--backup-host", "127.0.0.1", "--backup-port", str(backup.port)]
        self.process = subprocess.Popen(args, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=self.stderr)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.stderr.seek(0)
                self.fail("Live proxy exited: " + self.stderr.read().decode(errors="replace"))
            try:
                await self.api("/api/status")
                return
            except (OSError, asyncio.IncompleteReadError):
                await asyncio.sleep(.05)
        self.fail("Live proxy did not start")

    async def api(self, path):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.dashboard)
        try:
            writer.write(f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{self.dashboard}\r\n\r\n".encode())
            await writer.drain()
            raw = await asyncio.wait_for(reader.read(), 5)
            headers, body = raw.split(b"\r\n\r\n", 1)
            self.assertIn(b"200 OK", headers)
            return json.loads(body)
        finally:
            writer.close()
            await writer.wait_closed()

    async def miner(self, *, hot=True, configure=False):
        miner = Miner(self.port, hot=hot, configure=configure)
        self.miners.append(miner)
        return await miner.connect()

    async def test_hot_switch_routes_1000_valid_shares_without_asic_disconnect(self):
        await self.launch()
        miner = await self.miner()
        self.assertNotEqual(miner.job["params"][0], "shared-job")
        for _ in range(1000):
            self.assertTrue((await miner.submit())["result"])
        self.assertFalse(miner.closed.is_set())
        self.assertFalse(miner.reconnect.is_set())
        self.assertEqual((self.user_pool.accepted, self.fee_pool.accepted), (990, 10))
        self.assertEqual((self.user_pool.rejected, self.fee_pool.rejected), (0, 0))
        self.assertTrue(self.user_pool.validated_extranonces.isdisjoint(self.fee_pool.validated_extranonces))
        self.assertEqual(self.user_pool.connections, 1)
        self.assertEqual(self.fee_pool.connections, 1)
        methods = [msg["method"] for msg in miner.notifications]
        switches = [i for i, method in enumerate(methods) if method == "mining.set_extranonce"]
        self.assertGreater(len(switches), 0)
        for i in switches:
            self.assertEqual(methods[i:i + 3], ["mining.set_extranonce", "mining.set_difficulty", "mining.notify"])
            self.assertIs(miner.notifications[i + 2]["params"][8], True)
        metrics = await self.api("/api/metrics")
        self.assertEqual(metrics["accepted"], 1000)
        self.assertEqual(metrics["rejected"], 0)
        report = await self.api("/api/audit-export")
        self.assertTrue(report["devfee_enabled"])
        self.assertAlmostEqual(report["fee_percentage"], 1.0)

    async def test_legacy_miner_reconnects_and_rebuilds_pow_with_new_extranonce(self):
        await self.launch()
        miner = await self.miner(hot=False)
        sockets = 1
        for _ in range(100):
            self.assertTrue((await miner.submit())["result"])
            if miner.reconnect.is_set() or miner.closed.is_set():
                await miner.close()
                miner = await self.miner(hot=False)
                sockets += 1
        self.assertGreaterEqual(sockets, 3)
        self.assertEqual((self.user_pool.accepted, self.fee_pool.accepted), (99, 1))
        self.assertEqual((self.user_pool.rejected, self.fee_pool.rejected), (0, 0))
        self.assertTrue(self.user_pool.validated_extranonces.isdisjoint(self.fee_pool.validated_extranonces))

    async def test_hot_failover_preserves_asic_socket_and_proof_of_work(self):
        backup = await self.pool("33333333", "user.worker")
        await self.launch(fee=False, backup=backup)
        miner = await self.miner()
        self.assertTrue((await miner.submit())["result"])
        old_extra = miner.extra
        miner.job_event.clear()
        await self.user_pool.close()
        await asyncio.wait_for(miner.job_event.wait(), 5)
        self.assertNotEqual(miner.extra, old_extra)
        self.assertFalse(miner.closed.is_set())
        self.assertFalse(miner.reconnect.is_set())
        self.assertTrue((await miner.submit())["result"])
        self.assertEqual((self.user_pool.accepted, backup.accepted), (1, 1))
        self.assertEqual(backup.rejected, 0)

    async def test_local_gatekeeper_rejects_duplicates_unknown_jobs_and_bad_pow(self):
        await self.launch(fee=False)
        miner = await self.miner()
        params = miner.share_params()
        self.assertTrue((await miner.submit(params))["result"])
        duplicate = await miner.submit(params)
        self.assertIsNot(duplicate.get("result"), True)
        unknown = params.copy()
        unknown[1] = "unknown-job"
        self.assertIsNot((await miner.submit(unknown)).get("result"), True)
        bad = params.copy()
        bad[4] = next(f"{number:08x}" for number in range(10000)
                      if not meets_target(build_header(miner.extra, "00000000", f"{number:08x}"), SHARE_TARGET))
        self.assertIsNot((await miner.submit(bad)).get("result"), True)
        self.assertEqual((self.user_pool.accepted, self.user_pool.rejected), (1, 0))
        metrics = await self.api("/api/metrics")
        self.assertEqual(metrics["accepted"], 1)
        self.assertEqual(metrics["invalid_local"], 3)

    async def test_late_user_share_is_routed_to_original_upstream_during_fee_window(self):
        await self.launch()
        miner = await self.miner()
        user_job = miner.job
        for _ in range(200):
            self.assertTrue((await miner.submit())["result"])
            if miner.job["extra"] != user_job["extra"]:
                break
        else:
            self.fail("Fee window did not start")
        self.assertNotEqual(miner.job["params"][0], user_job["params"][0])
        user_before, fee_before = self.user_pool.accepted, self.fee_pool.accepted
        late_share = miner.share_params(user_job)
        self.assertTrue((await miner.submit(late_share))["result"])
        self.assertEqual(self.user_pool.accepted, user_before + 1)
        self.assertEqual(self.fee_pool.accepted, fee_before)
        self.assertEqual((self.user_pool.rejected, self.fee_pool.rejected), (0, 0))
        self.assertFalse(miner.closed.is_set())

    async def test_version_rolling_mask_is_replayed_to_fee_pool_and_pow_remains_valid(self):
        await self.launch()
        miner = await self.miner(configure=True)
        for _ in range(100):
            self.assertTrue((await miner.submit())["result"])
        self.assertEqual((self.user_pool.accepted, self.fee_pool.accepted), (99, 1))
        self.assertEqual((self.user_pool.rejected, self.fee_pool.rejected), (0, 0))
        self.assertTrue(self.user_pool.configure_requests)
        self.assertTrue(self.fee_pool.configure_requests)
        user_mask = self.user_pool.configure_requests[0][1]["version-rolling.mask"]
        fee_mask = self.fee_pool.configure_requests[0][1]["version-rolling.mask"]
        self.assertEqual(user_mask, fee_mask)
        self.assertFalse(miner.closed.is_set())

    async def test_difficulty_change_applies_to_next_job_not_cached_job(self):
        await self.launch()
        miner = await self.miner()
        original_difficulty = miner.difficulty
        for _ in range(200):
            self.assertTrue((await miner.submit())["result"])
            if miner.job["extra"].startswith("222222"):
                break
        else:
            self.fail("Fee window did not start")
        # Pool updates vardiff while its last issued job is still the only one.
        # It does not apply to that existing job until a fresh notify arrives.
        for writer in self.user_pool.writers:
            writer.write(encode({"id": None, "method": "mining.set_difficulty",
                                 "params": [DIFFICULTY * 2]}))
            await writer.drain()
        await asyncio.sleep(.05)
        self.assertTrue((await miner.submit())["result"])
        self.assertEqual(miner.job["extra"][:6], "111111")
        self.assertEqual(miner.difficulty, original_difficulty)
        self.assertTrue((await miner.submit())["result"])
        self.assertEqual(self.user_pool.rejected, 0)

    async def test_user_failure_during_fee_window_moves_to_backup(self):
        backup = await self.pool("33333333", "user.worker")
        await self.launch(backup=backup)
        miner = await self.miner()
        for _ in range(200):
            self.assertTrue((await miner.submit())["result"])
            if miner.extra.startswith("222222"):
                break
        else:
            self.fail("Fee window did not start")
        miner.job_event.clear()
        await self.user_pool.close()
        await asyncio.wait_for(miner.job_event.wait(), 5)
        self.assertTrue(miner.extra.startswith("333333"))
        self.assertFalse(miner.closed.is_set())
        self.assertTrue((await miner.submit())["result"])
        self.assertEqual(backup.accepted, 1)

    async def test_required_fee_outage_stops_all_miners(self):
        await self.launch(require_fee=True)
        first = await self.miner()
        second = await self.miner()
        await self.fee_pool.close()
        await asyncio.wait_for(asyncio.gather(first.closed.wait(), second.closed.wait()), 4)
        self.assertTrue((await self.api("/api/status"))["fee_policy_blocked"])

    async def test_required_fee_rejections_stop_miner_with_connected_pool(self):
        await self.launch(require_fee=True)
        miner = await self.miner()
        self.fee_pool.reject_submits = True
        self.assertTrue((await miner.submit())["result"])
        self.assertTrue(miner.extra.startswith("222222"))
        self.assertFalse((await miner.submit())["result"])
        await asyncio.wait_for(miner.closed.wait(), 4)
        self.assertTrue((await self.api("/api/status"))["fee_policy_blocked"])


if __name__ == "__main__":
    unittest.main()
