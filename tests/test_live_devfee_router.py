"""PoW-checked integration tests through the actual proxy.py entrypoint.

The pools are deliberately local and synthetic. They validate each submitted
80-byte Bitcoin header independently of the router's own PoW validator.
"""
import asyncio
import json
import hashlib
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import unittest
from fractions import Fraction
from pathlib import Path
from cryptography.fernet import Fernet

from lab.pool import (COINB1, COINB2, NBITS, NTIME, PREVHASH, SHARE_TARGET,
                      VERSION, build_header, find_nonce)
from lab.pow import compact_target, meets_target


ROOT = Path(__file__).resolve().parent.parent
DIFFICULTY = compact_target(0x1D00FFFF) / SHARE_TARGET


def encode(value):
    return json.dumps(value, separators=(",", ":")).encode() + b"\n"


def checked_header(extra, ex2, nonce, version_bits=None, mask=0x1FFFE000):
    """Independent fixture construction, including the negotiated BIP310 mask."""
    coinbase = bytes.fromhex(COINB1 + extra + ex2 + COINB2)
    root = hashlib.sha256(hashlib.sha256(coinbase).digest()).digest()
    header = (bytes.fromhex(VERSION)[::-1] + bytes.fromhex(PREVHASH) + root +
              bytes.fromhex(NTIME)[::-1] + bytes.fromhex(NBITS)[::-1] + bytes.fromhex(nonce)[::-1])
    if version_bits is None:
        return header
    bits = int(version_bits, 16)
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
        self.allowed_workers = {worker}
        self.authorize_requests = []
        self.accepted = 0
        self.rejected = 0
        self.connections = 0
        self.validated_extranonces = set()
        self.writers = set()
        self.errors = []
        self.configure_requests = []
        self.subscribe_requests = []
        self.reject_submits = False
        self.submit_delay = 0
        self.extra_size = 4
        self.version_mask = '1fffe000'
        self.difficulty = DIFFICULTY
        self.share_target = SHARE_TARGET

    async def start(self, ssl_context=None):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0, ssl=ssl_context)
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
        rolling_mask = None
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
                        requested_mask = params[1].get('version-rolling.mask','ffffffff')
                        rolling_mask = int(requested_mask, 16) & int(self.version_mask, 16)
                        response["result"]["version-rolling.mask"] = f'{rolling_mask:08x}'
                elif method == "mining.subscribe":
                    self.subscribe_requests.append(params)
                    subscribed = True
                    response["result"] = [[["mining.notify", "session"]], extra, self.extra_size]
                elif method == "mining.extranonce.subscribe":
                    response["result"] = True
                elif method == "mining.authorize":
                    self.authorize_requests.append(params[0])
                    response["result"] = subscribed and params[0] in self.allowed_workers
                    if response["result"]:
                        authorized.add(params[0])
                elif method == "mining.submit":
                    # Both pools deliberately use the SAME original job id.
                    # Only routing and the original session extranonce differ.
                    worker, job, ex2, ntime, nonce = params[:5]
                    key = (job, ex2, ntime, nonce, *params[5:])
                    version_bits = params[5] if len(params) > 5 else None
                    valid_version = ((version_bits is None and rolling_mask is None) or
                                     (version_bits is not None and rolling_mask is not None and
                                      not int(version_bits, 16) & ~rolling_mask))
                    valid = (valid_version and not self.reject_submits and len(ex2) == self.extra_size * 2 and worker in authorized and job == "shared-job" and
                             ntime == NTIME and key not in seen and
                             meets_target(checked_header(extra, ex2, nonce, version_bits, rolling_mask), self.share_target))
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
                if method == "mining.submit" and self.submit_delay:
                    await asyncio.sleep(self.submit_delay)
                writer.write(encode(response))
                if method == "mining.authorize" and response["result"]:
                    writer.write(encode({"id": None, "method": "mining.set_difficulty", "params": [self.difficulty]}))
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
    def __init__(self, port, *, hot=True, configure=False, password="x"):
        self.port = port
        self.hot = hot
        self.next_id = 0
        self.pending = {}
        self.notifications = []
        self.extra = None
        self.extra_size = 4
        self.difficulty = None
        self.job = None
        self.job_event = asyncio.Event()
        self.reconnect = asyncio.Event()
        self.closed = asyncio.Event()
        self.nonces = {}
        self.configure = configure
        self.version_bits = None
        self.password = password
        self.subscribe_params = ["chefas-proxy-integration/1"]

    async def connect(self):
        self.reader, self.writer = await asyncio.open_connection("127.0.0.1", self.port)
        self.reader_task = asyncio.create_task(self.read_loop())
        if self.configure:
            configured = await self.request("mining.configure", [["version-rolling"], {
                "version-rolling.mask": "1fffe000", "version-rolling.min-bit-count": 2}])
            if configured.get("error") or configured.get('result',{}).get('version-rolling') is not True:
                raise AssertionError(configured)
            self.version_bits = "00002000"
            self.version_mask = configured['result']['version-rolling.mask']
        subscribed = await self.request("mining.subscribe", self.subscribe_params)
        if subscribed.get("error"):
            raise AssertionError(subscribed)
        self.extra = subscribed["result"][1]
        self.extra_size = subscribed['result'][2]
        if self.hot:
            extension = await self.request("mining.extranonce.subscribe", [])
            if extension.get("result") is not True:
                raise AssertionError(extension)
        authorized = await self.request("mining.authorize", ["user.worker", self.password])
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
                        self.extra_size = params[1]
                    elif method == "mining.set_difficulty":
                        self.difficulty = params[0]
                    elif method == "mining.notify":
                        self.job = {"params": params, "extra": self.extra, "diff": self.difficulty, 'size': self.extra_size}
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
        ex2 = '00' * job.get('size',4)
        threshold=compact_target(0x1D00FFFF) // Fraction(str(job['diff']))
        if self.version_bits is None and len(extra) == 8 and len(ex2) == 8 and job['diff']==DIFFICULTY:
            nonce = find_nonce(extra, start=start)
        else:
            nonce = next((f"{number:08x}" for number in range(start, start + 1000000)
                          if meets_target(checked_header(extra, ex2, f"{number:08x}", self.version_bits), threshold)), None)
            if nonce is None:
                raise AssertionError("No test share found")
        self.nonces[extra] = int(nonce, 16) + 1
        params = ["user.worker", job["params"][0], ex2, NTIME, nonce]
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
        self.history = tempfile.TemporaryDirectory()

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
        self.history.cleanup()

    async def pool(self, extra, worker, ssl_context=None):
        pool = await CheckedPool(extra, worker).start(ssl_context)
        self.pools.append(pool)
        return pool

    async def launch(self, *, fee=True, backup=None, require_fee=False, admin_token=None, debug=False,
                     miner_timeout=600, handshake_timeout=30, jitter=0, connect_interval=0,
                     job_ttl=900, job_gc_interval=60, ssl_user=False, ssl_fee=False,
                     user_extra_size=4, fee_extra_size=4, user_mask='1fffe000', asic_min=0,
                     fee_min_shares=0,fee_template=None,fee_difficulty_multiplier=1,
                     aggregate=False, connect_timeout=None, failover_recovery=30):
        self.admin_token = admin_token
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(ROOT/'tests'/'fixtures'/'lab-cert.pem', ROOT/'tests'/'fixtures'/'lab-key.pem')
        self.user_pool = await self.pool("11111111", "user.worker", context if ssl_user else None)
        self.user_pool.extra_size = user_extra_size
        self.user_pool.version_mask = user_mask
        if fee:
            self.fee_pool = await self.pool("22222222", "fee.worker", context if ssl_fee else None)
            self.fee_pool.extra_size = fee_extra_size
            self.fee_pool.difficulty=DIFFICULTY*fee_difficulty_multiplier
            self.fee_pool.share_target=SHARE_TARGET//fee_difficulty_multiplier
            if fee_template:
                self.fee_pool.allowed_workers={f'fee.worker_{slot}' for slot in range(1,4)}
        self.port, self.dashboard = unused_ports(2)
        entrypoint = os.environ.get("CHEFAS_PROXY_TEST_ENTRYPOINT", str(ROOT / "launch.py"))
        args = [sys.executable, entrypoint, "--active-router", "--validate-pow",
                "--env-file", "", "--log-dir", str(Path(self.history.name) / "logs"),
                "--history-file", str(Path(self.history.name) / "history.json"),
                "--control-state", str(Path(self.history.name) / "control.json"),
                '--compatibility-state', str(Path(self.history.name)/'compatibility.json'),
                "--pool-host", "stratum+ssl://localhost" if ssl_user else "127.0.0.1", "--pool-port", str(self.user_pool.port),
                "--listen-port", str(self.port), "--dashboard-port", str(self.dashboard),
                "--connect-timeout", str(connect_timeout or (3 if ssl_user or ssl_fee else 1)),
                "--reconnect-min-seconds", "0",
                "--miner-timeout", str(miner_timeout), "--handshake-timeout", str(handshake_timeout),
                "--devfee-jitter-seconds", str(jitter), '--devfee-connect-interval', str(connect_interval),
                '--job-ttl', str(job_ttl), '--job-gc-interval', str(job_gc_interval)]
        args += ['--asic-extranonce2-min',str(asic_min)]
        if aggregate:
            args += ['--aggregate']
        if fee_template:
            args += ['--devfee-worker-template',fee_template]
        if fee:
            args += ["--devfee-host", "stratum+ssl://localhost" if ssl_fee else "127.0.0.1", "--devfee-port", str(self.fee_pool.port),
                     "--devfee-worker", "fee.worker", "--fee-min-work", str(fee_min_shares*DIFFICULTY)]
        if require_fee:
            # Include both real TCP handshakes on a loaded Windows runner.
            # Exact deadline arithmetic is covered by test_fee_health.
            args += ["--require-devfee", "--fee-grace-seconds", "2"]
        if backup:
            args += ["--backup-host", "127.0.0.1", "--backup-port", str(backup.port),
                     "--failover-recovery", str(failover_recovery)]
        if debug:
            args += ["--log-level", "DEBUG", "--debug-stratum"]
        if ssl_user or ssl_fee:
            args += ['--tls-ca-file', str(ROOT/'tests'/'fixtures'/'lab-cert.pem')]
        environment = dict(os.environ)
        if environment.get('CHEFAS_PROXY_TEST_EXECUTABLE'):
            args = [environment['CHEFAS_PROXY_TEST_EXECUTABLE'],*args[2:]]
        environment["CHEFAS_PROXY_SECRETS_KEY"] = Fernet.generate_key().decode()
        environment["CHEFAS_PROXY_USER_PASSWORD"] = "x"
        environment["CHEFAS_PROXY_DEVFEE_PASSWORD"] = "x"
        environment.pop("CHEFAS_PROXY_ADMIN_TOKEN", None)
        if admin_token:
            environment["CHEFAS_PROXY_ADMIN_TOKEN"] = admin_token
        self.process = subprocess.Popen(args, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=self.stderr, env=environment)
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

    async def api(self, path, data=None, expected=200):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.dashboard)
        try:
            body = json.dumps(data).encode() if data is not None else b""
            auth = f"Authorization: Bearer {self.admin_token}\r\n" if self.admin_token else ""
            writer.write(f"{'POST' if data is not None else 'GET'} {path} HTTP/1.1\r\nHost: 127.0.0.1:{self.dashboard}\r\n{auth}Content-Length: {len(body)}\r\n\r\n".encode() + body)
            await writer.drain()
            raw = await asyncio.wait_for(reader.read(), 5)
            headers, body = raw.split(b"\r\n\r\n", 1)
            self.assertIn(str(expected).encode() + b" ", headers)
            if path == "/metrics":
                self.assertIn(b"text/plain; version=0.0.4", headers)
                return body.decode()
            return json.loads(body)
        finally:
            writer.close()
            await writer.wait_closed()

    async def miner(self, *, hot=True, configure=False, password="x"):
        miner = Miner(self.port, hot=hot, configure=configure, password=password)
        self.miners.append(miner)
        return await miner.connect()

    async def test_aggregate_reconnects_and_multiplexes_valid_shares(self):
        from pow_validator import build_stratum_header

        await self.launch(fee=False, aggregate=True)
        reader, writer = await asyncio.open_connection('127.0.0.1', self.port)
        try:
            writer.write(encode({'id': 1, 'method': 'mining.subscribe',
                                 'params': ['chefas-proxy-integration/1']}))
            subscribed = json.loads(await asyncio.wait_for(reader.readline(), 5))
            self.assertEqual(subscribed['result'][2], 4)
            self.assertEqual(self.user_pool.connections, 0)
            writer.write(encode({'id': 2, 'method': 'mining.authorize',
                                 'params': ['user.worker', 'x']}))
            reconnect = json.loads(await asyncio.wait_for(reader.readline(), 5))
            self.assertEqual(reconnect['method'], 'client.reconnect')
            self.assertEqual(await asyncio.wait_for(reader.readline(), 5), b'')
        finally:
            writer.close()
            await writer.wait_closed()

        miners = [await self.miner(hot=False) for _ in range(3)]
        self.assertEqual(self.user_pool.connections, 2)  # one probe, one shared connection
        self.assertEqual(len({miner.extra for miner in miners}), 3)
        self.assertTrue(all(miner.extra_size == 2 for miner in miners))

        def proof(miner, *, pool_valid, start=0):
            job = miner.job['params']
            local_target = compact_target(0x1D00FFFF) // Fraction(str(miner.difficulty))
            ex2 = '00' * miner.extra_size
            for number in range(start, start + 100000):
                nonce = f'{number:08x}'
                header = build_stratum_header(job, miner.extra, ex2, NTIME, nonce)
                if meets_target(header, local_target) and meets_target(header, SHARE_TARGET) == pool_valid:
                    return ['user.worker', job[0], ex2, NTIME, nonce]
            self.fail('could not find suitable share')

        local = await miners[0].submit(proof(miners[0], pool_valid=False))
        self.assertIs(local['result'], True)
        self.assertEqual(self.user_pool.accepted, 0)
        shares = [proof(miner, pool_valid=True) for miner in miners]
        results = await asyncio.gather(*(miner.submit(share) for miner, share in zip(miners, shares)))
        self.assertTrue(all(result['result'] is True for result in results))
        self.assertEqual(self.user_pool.accepted, 3)
        self.assertEqual(self.user_pool.rejected, 0)
        await miners[0].close()
        extra_share = proof(miners[1], pool_valid=True, start=int(shares[1][4], 16) + 1)
        self.assertIs((await miners[1].submit(extra_share))['result'], True)
        self.assertEqual(self.user_pool.accepted, 4)
        self.assertEqual(len(self.user_pool.writers), 1)
        self.assertEqual(self.user_pool.connections, 2)
        await miners[1].close()
        await miners[2].close()
        deadline = time.monotonic() + 3
        while self.user_pool.writers and time.monotonic() < deadline:
            await asyncio.sleep(.01)
        self.assertFalse(self.user_pool.writers)

    async def test_aggregate_fee_rolls_versions_on_shared_sockets_without_reconnecting(self):
        from pow_validator import build_stratum_header

        await self.launch(aggregate=True, user_extra_size=8, fee_extra_size=8,
                          fee_min_shares=98.5, user_mask='00006000', connect_timeout=5)
        miners = [await self.miner(hot=False, configure=True) for _ in range(2)]
        self.assertEqual([miner.version_mask for miner in miners], ['00006000'] * 2)
        prefixes = [miner.extra for miner in miners]
        self.assertEqual(len(set(prefixes)), 2)
        deadline = time.monotonic() + 5
        while not self.fee_pool.authorize_requests and time.monotonic() < deadline:
            await asyncio.sleep(.02)
        self.assertEqual(self.fee_pool.authorize_requests, ['fee.worker'])
        # Allow the shared authorize response and job to reach both subscribers.
        await asyncio.sleep(.3)
        nonces = {}

        def proof(miner, job=None):
            job = job or miner.job
            params = job['params']
            ex2 = '00' * job['size']
            key = (miner.extra, params[0])
            for number in range(nonces.get(key, 0), 1000000):
                nonce = f'{number:08x}'
                header = build_stratum_header(params, miner.extra, ex2, NTIME, nonce,
                                             miner.version_bits, miner.version_mask)
                if meets_target(header, SHARE_TARGET):
                    nonces[key] = number + 1
                    return ['user.worker', params[0], ex2, NTIME, nonce, miner.version_bits]
            self.fail('could not find pool-valid share')

        user_job = miners[0].job
        invalid = proof(miners[0], user_job)
        invalid[-1] = '00008000'
        self.assertIs((await miners[0].submit(invalid))['result'], False)
        for _ in range(99):
            self.assertIs((await miners[0].submit(proof(miners[0], user_job)))['result'], True)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not any(m.job['params'][0].startswith('f') for m in miners):
            await asyncio.sleep(.02)
        selected = [m for m in miners if m.job['params'][0].startswith('f')]
        self.assertEqual(len(selected), 1)
        self.assertIs((await selected[0].submit(proof(selected[0])))['result'], True)
        self.assertEqual((self.user_pool.accepted, self.fee_pool.accepted), (99, 1))
        self.assertEqual((len(self.user_pool.writers), len(self.fee_pool.writers)), (1, 1))
        self.assertEqual(self.user_pool.authorize_requests, ['user.worker'])
        self.assertEqual(self.fee_pool.configure_requests[0][1]['version-rolling.mask'], '00006000')
        self.assertEqual((self.user_pool.rejected, self.fee_pool.rejected), (0, 0))
        self.assertEqual([miner.extra for miner in miners], prefixes)
        self.assertFalse(any(miner.closed.is_set() for miner in miners))
        self.assertAlmostEqual((await self.api('/api/audit-export'))['fee_percentage'], 1.0)
        self.assertIs((await miners[0].submit(proof(miners[0], user_job)))['result'], True)
        self.assertEqual(self.user_pool.accepted, 100)
        self.user_pool.reject_submits = True
        self.assertIs((await miners[0].submit(proof(miners[0], user_job)))['result'], False)
        self.assertEqual(self.user_pool.rejected, 1)
        metrics = await self.api('/metrics')
        self.assertIn('chefas_proxy_active_workers 2\n', metrics)
        self.assertIn('chefas_proxy_upstream_sockets{mode="shared"} 2\n', metrics)
        for network in ('lan', 'wan'):
            for direction in ('rx', 'tx'):
                self.assertRegex(metrics, rf'chefas_proxy_{network}_bytes_total\{{direction="{direction}"\}} [1-9][0-9]*\n')
        for route, pool, accepted in (('user', self.user_pool, 100), ('fee', self.fee_pool, 1)):
            labels = f'pool="127.0.0.1:{pool.port}",route="{route}"'
            rejected = int(route == 'user')
            self.assertIn(f'chefas_proxy_shares_accepted_total{{{labels}}} {accepted}\n', metrics)
            self.assertIn(f'chefas_proxy_shares_rejected_total{{{labels}}} {rejected}\n', metrics)
            self.assertIn(f'chefas_proxy_upstream_latency_seconds_count{{{labels}}} {accepted + rejected}\n', metrics)
        self.assertIn(f'chefas_proxy_shares_devfee_total{{pool="127.0.0.1:{self.fee_pool.port}",route="fee"}} 1\n',
                      metrics)

        journal = (await self.api('/api/audit-export'))['persistent_work']
        totals = {entry['route']: entry for entry in journal['totals']}
        self.assertIsNone(journal['error'])
        for route, accepted in (('user', 100), ('fee', 1)):
            self.assertEqual(totals[route]['accepted'], accepted)
            self.assertEqual(totals[route]['rejected'], int(route == 'user'))
            self.assertEqual(Fraction(totals[route]['accepted_difficulty']), accepted * Fraction(str(DIFFICULTY)))

        self.process.kill()
        await asyncio.wait_for(asyncio.to_thread(self.process.wait), 5)
        await self.launch(fee=False, aggregate=True)
        restored = await self.api('/api/audit-export')
        self.assertEqual(restored['persistent_work'], journal)
        self.assertAlmostEqual(restored['fee_percentage'], 100 / 101)

    async def test_aggregate_fee_disconnect_restores_user_job_on_same_socket(self):
        from pow_validator import build_stratum_header

        await self.launch(aggregate=True, user_extra_size=8, fee_extra_size=8, connect_timeout=5)
        miner = await self.miner(hot=False)
        prefix = miner.extra
        deadline = time.monotonic() + 5
        while not self.fee_pool.authorize_requests and time.monotonic() < deadline:
            await asyncio.sleep(.02)
        self.assertTrue(self.fee_pool.authorize_requests)
        await asyncio.sleep(.3)
        job = miner.job['params']
        extra2 = '00' * miner.extra_size
        accepted = 0
        for i in range(1000000):
            nonce = f'{i:08x}'
            if not meets_target(build_stratum_header(job, prefix, extra2, NTIME, nonce), SHARE_TARGET):
                continue
            self.assertIs((await miner.submit(['user.worker', job[0], extra2, NTIME, nonce]))['result'], True)
            accepted += 1
            if accepted == 98:
                break
        self.assertEqual(accepted, 98)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not miner.job['params'][0].startswith('f'):
            await asyncio.sleep(.02)
        self.assertTrue(miner.job['params'][0].startswith('f'))
        for writer in tuple(self.fee_pool.writers):
            writer.close()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not miner.job['params'][0].startswith('u'):
            await asyncio.sleep(.02)
        self.assertTrue(miner.job['params'][0].startswith('u'))
        self.assertFalse(miner.closed.is_set())
        self.assertEqual(miner.extra, prefix)
        self.assertEqual(self.fee_pool.accepted, 0)

    async def test_aggregate_failover_moves_group_before_socket_break_and_recovers(self):
        from pow_validator import build_stratum_header

        backup = await self.pool('33333333', 'user.worker')
        backup.extra_size = 8
        backup.version_mask = '00006000'
        await self.launch(fee=False, aggregate=True, backup=backup, user_extra_size=8,
                          failover_recovery=1, connect_timeout=5)
        miners = await asyncio.gather(*(self.miner(hot=False, configure=True) for _ in range(2)))
        prefixes = [miner.extra for miner in miners]
        self.assertEqual([miner.version_mask for miner in miners], ['00006000'] * 2)
        first = miners[0]
        old_job = first.job
        nonces = {}

        def proof(miner, job):
            params = job['params']
            extra2 = '00' * job['size']
            key = (miner.extra, params[2], params[7])
            for number in range(nonces.get(key, 0), 1000000):
                nonce = f'{number:08x}'
                header = build_stratum_header(params, miner.extra, extra2, NTIME, nonce,
                                              miner.version_bits, miner.version_mask)
                if meets_target(header, SHARE_TARGET):
                    nonces[key] = number + 1
                    return ['user.worker', params[0], extra2, NTIME, nonce, miner.version_bits]
            self.fail('could not find valid proof')

        async def wait_until(predicate):
            deadline = time.monotonic() + 6
            while not predicate() and time.monotonic() < deadline:
                await asyncio.sleep(.02)
            self.assertTrue(predicate())

        await wait_until(lambda: backup.authorize_requests == ['user.worker'])
        self.user_pool.reject_submits = True
        for _ in range(3):
            self.assertIs((await first.submit(proof(first, old_job)))['result'], False)
        await wait_until(lambda: all(m.job['params'][0].startswith('b') for m in miners))
        self.assertEqual(len(self.user_pool.writers), 1)
        self.assertEqual(len(backup.writers), 1)
        self.assertTrue(all(reply['result'] for reply in await asyncio.gather(
            *(m.submit(proof(m, m.job)) for m in miners))))
        self.assertEqual(backup.accepted, 2)
        self.user_pool.reject_submits = False
        self.assertIs((await first.submit(proof(first, old_job)))['result'], True)
        self.assertEqual(self.user_pool.accepted, 1)
        self.assertTrue(first.job['params'][0].startswith('b'))
        notice = {'id': None, 'method': 'mining.notify', 'params': [
            'shared-job', PREVHASH, COINB1, COINB2, [], VERSION, NBITS, NTIME, True]}
        for writer in tuple(self.user_pool.writers):
            writer.write(encode(notice))
            await writer.drain()
        await wait_until(lambda: all(m.job['params'][0].startswith('u') for m in miners))
        self.assertEqual([miner.extra for miner in miners], prefixes)
        self.assertFalse(any(miner.closed.is_set() for miner in miners))
        self.assertIs((await first.submit(proof(first, first.job)))['result'], True)
        self.assertEqual(self.user_pool.accepted, 2)
        for writer in tuple(self.user_pool.writers):
            writer.close()
        await wait_until(lambda: all(m.job['params'][0].startswith('b') for m in miners))
        await wait_until(lambda: all(m.job['params'][0].startswith('u') for m in miners))
        self.assertEqual(self.user_pool.authorize_requests, ['user.worker'] * 2)
        self.assertFalse(any(miner.closed.is_set() for miner in miners))

    async def test_scanners_dropped_before_upstream_connect(self):
        await self.launch(fee=False)
        for payload in (b'GET / HTTP/1.1\r\n', b'\x16\x03\x01', b'\xff',
                        b'{"id":1,"method":"mining.subscribe","params":["'+b'x'*4096):
            reader, writer = await asyncio.open_connection('127.0.0.1', self.port)
            try:
                writer.write(payload)
                await writer.drain()
                self.assertEqual(await asyncio.wait_for(reader.read(), 2), b'')
            finally:
                writer.close()
                await writer.wait_closed()
        self.assertEqual(self.user_pool.connections, 0)
        metrics = await self.api('/api/metrics')
        self.assertEqual(metrics['protocol_drops'], 4)

    async def test_fee_reuses_exact_agent_but_not_other_pool_session_id(self):
        await self.launch()
        miner=Miner(self.port)
        miner.subscribe_params=['cgminer/4.11.1 vendor/custom-version','user-pool-session-resume-id']
        self.miners.append(miner)
        await miner.connect()
        self.assertEqual(self.user_pool.subscribe_requests[0],miner.subscribe_params)
        self.assertEqual(self.fee_pool.subscribe_requests[0],[miner.subscribe_params[0]])
        self.assertTrue((await miner.submit())['result'])

    async def test_extranonce_width_translation_preserves_pow_across_pools(self):
        await self.launch(user_extra_size=6,fee_extra_size=8)
        miner = await self.miner(configure=True)
        self.assertEqual(miner.extra_size,4)
        for _ in range(100):
            self.assertTrue((await miner.submit())['result'])
        self.assertEqual((self.user_pool.accepted,self.fee_pool.accepted),(99,1))
        self.assertEqual(self.user_pool.rejected+self.fee_pool.rejected,0)
        self.assertEqual(miner.extra_size,4)
        self.assertFalse(miner.closed.is_set())

    async def test_incompatible_fee_nonce_width_keeps_optional_user_mining(self):
        await self.launch(fee_extra_size=2,asic_min=4)
        miner = await self.miner()
        for _ in range(5):
            self.assertTrue((await miner.submit())['result'])
        self.assertEqual(self.user_pool.accepted,5)
        self.assertEqual(self.fee_pool.accepted,0)
        self.assertFalse(miner.closed.is_set())

    async def test_fee_negotiates_exact_user_mask_instead_of_wider_superset(self):
        await self.launch(user_mask='00006000')
        miner = await self.miner(configure=True)
        for _ in range(100):
            self.assertTrue((await miner.submit())['result'])
        self.assertEqual(self.fee_pool.configure_requests[0][1]['version-rolling.mask'],'00006000')
        self.assertEqual((self.user_pool.accepted,self.fee_pool.accepted),(99,1))
        self.assertEqual(self.user_pool.rejected+self.fee_pool.rejected,0)

    async def test_minimum_difficulty_reconfigure_does_not_forget_version_rolling(self):
        await self.launch()
        miner = await self.miner(configure=True)
        await miner.request('mining.configure',[['minimum-difficulty'],{'minimum-difficulty.value':1}])
        for _ in range(100):
            self.assertTrue((await miner.submit())['result'])
        self.assertEqual((self.user_pool.accepted,self.fee_pool.accepted),(99,1))
        self.assertEqual(self.user_pool.rejected+self.fee_pool.rejected,0)
        self.assertFalse(miner.closed.is_set())

    async def test_client_disconnect_after_hot_switch_learns_persistent_reconnect(self):
        await self.launch()
        miner = await self.miner()
        self.assertTrue((await miner.submit())['result'])
        self.assertTrue(miner.extra.startswith('222222'))
        await miner.close()
        for _ in range(100):
            if (await self.api('/api/metrics'))['compatibility_fallbacks']:
                break
            await asyncio.sleep(.02)
        self.assertEqual((await self.api('/api/metrics'))['compatibility_fallbacks'],1)
        self.assertTrue((Path(self.history.name)/'compatibility.json').exists())
        replacement = await self.miner()
        self.assertTrue((await replacement.submit())['result'])
        await asyncio.wait_for(replacement.reconnect.wait(),3)
        self.assertTrue(replacement.reconnect.is_set())

    async def test_jitter_warmup_keeps_user_mining_and_replays_version_rolling(self):
        await self.launch(jitter=.5, connect_interval=.05, require_fee=True)
        miner = await self.miner(configure=True)
        self.assertTrue(miner.extra.startswith('111111'))
        self.assertTrue((await miner.request('mining.ping', []))['result'])
        for _ in range(150):
            if self.fee_pool.configure_requests and self.fee_pool.connections:
                break
            await asyncio.sleep(.02)
        self.assertEqual(self.fee_pool.connections,1)
        # Wait for asynchronous authorization/notify before exercising the fee path.
        await asyncio.sleep(.1)
        for _ in range(30):
            self.assertTrue((await miner.submit())['result'])
            if self.fee_pool.accepted:
                break
        self.assertGreater(self.fee_pool.accepted,0)
        self.assertEqual(self.user_pool.rejected+self.fee_pool.rejected,0)
        self.assertFalse(miner.closed.is_set())
        self.assertFalse((await self.api('/api/status'))['fee_policy_blocked'])

    async def test_jitter_supervisor_retries_after_fee_rpc_timeout(self):
        await self.launch(jitter=.2)
        miner = await self.miner()
        for _ in range(100):
            if self.fee_pool.connections:
                break
            await asyncio.sleep(.02)
        await asyncio.sleep(.1)
        for _ in range(30):
            self.assertTrue((await miner.submit())['result'])
            if miner.extra.startswith('222222'):
                break
        self.assertTrue(miner.extra.startswith('222222'))
        self.fee_pool.submit_delay = 2
        self.assertIsNot((await miner.submit()).get('result'),True)
        self.fee_pool.submit_delay = 0
        for _ in range(400):
            if self.fee_pool.connections >= 2:
                break
            await asyncio.sleep(.02)
        self.assertGreaterEqual(self.fee_pool.connections,2)
        self.assertFalse(miner.closed.is_set())
        self.assertTrue((await miner.request('mining.ping',[]))['result'])

    async def test_expired_job_rejected_before_background_gc(self):
        await self.launch(fee=False, job_ttl=.2, job_gc_interval=60)
        miner = await self.miner()
        await asyncio.sleep(.3)
        self.assertFalse((await miner.submit())['result'])
        self.assertEqual(self.user_pool.accepted,0)
        self.assertEqual((await self.api('/api/metrics'))['unknown_job_local'],1)

    async def test_inflight_accepted_share_survives_job_gc(self):
        await self.launch(fee=False, job_ttl=.3, job_gc_interval=.1)
        miner = await self.miner()
        self.user_pool.submit_delay = .5
        self.assertTrue((await miner.submit())['result'])
        self.assertEqual((await self.api('/api/metrics'))['accepted'],1)

    async def test_two_ssl_url_upstreams_validate_pow(self):
        await self.launch(ssl_user=True, ssl_fee=True)
        try:
            miner = await self.miner(configure=True)
        except ConnectionError:
            log = (Path(self.history.name)/'logs'/'proxy.log').read_text(encoding='utf-8')
            self.fail('TLS handshake failed: '+repr(await self.api('/api/metrics'))+' '+log)
        for _ in range(100):
            self.assertTrue((await miner.submit())['result'])
        self.assertEqual((self.user_pool.accepted,self.fee_pool.accepted),(99,1))
        self.assertEqual(self.user_pool.rejected+self.fee_pool.rejected,0)
        self.assertFalse(miner.closed.is_set())

    async def test_plain_user_and_ssl_fee_have_independent_transport(self):
        await self.launch(ssl_fee=True)
        miner = await self.miner()
        for _ in range(100):
            self.assertTrue((await miner.submit())['result'])
        self.assertEqual((self.user_pool.accepted,self.fee_pool.accepted),(99,1))
        self.assertEqual(self.user_pool.rejected+self.fee_pool.rejected,0)

    async def test_partial_handshake_reaped_without_pool_connection(self):
        await self.launch(fee=False, handshake_timeout=.3)
        reader, writer = await asyncio.open_connection('127.0.0.1', self.port)
        try:
            writer.write(b'{"id":1')
            await writer.drain()
            self.assertEqual(await asyncio.wait_for(reader.read(), 2), b'')
            self.assertEqual(self.user_pool.connections, 0)
            metrics = await self.api('/api/metrics')
            self.assertEqual(metrics['handshake_timeouts'], 1)
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_pool_notifications_do_not_keep_zombie_asic_alive(self):
        await self.launch(fee=False, miner_timeout=.5)
        miner = await self.miner()
        async def notify_forever():
            while not miner.closed.is_set():
                for writer in list(self.user_pool.writers):
                    writer.write(encode({'id':None,'method':'mining.notify','params':[
                        'shared-job',PREVHASH,COINB1,COINB2,[],VERSION,NBITS,NTIME,True]}))
                    await writer.drain()
                await asyncio.sleep(.05)
        task = asyncio.create_task(notify_forever())
        try:
            await asyncio.wait_for(miner.closed.wait(), 3)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        metrics = await self.api('/api/metrics')
        self.assertEqual(metrics['zombie_reaped'], 1)
        for _ in range(50):
            if not self.user_pool.writers:
                break
            await asyncio.sleep(.02)
        self.assertFalse(self.user_pool.writers)

    async def test_authenticated_ping_keeps_session_alive_then_reaps(self):
        await self.launch(fee=False, miner_timeout=.5)
        miner = await self.miner()
        for _ in range(8):
            self.assertTrue((await miner.request('mining.ping', []))['result'])
            await asyncio.sleep(.15)
        self.assertFalse(miner.closed.is_set())
        await asyncio.wait_for(miner.closed.wait(), 3)
        self.assertEqual((await self.api('/api/metrics'))['zombie_reaped'], 1)

    async def test_reaper_interrupts_pending_upstream_rpc_and_releases_capacity(self):
        await self.launch(fee=False, miner_timeout=.4)
        miner = await self.miner()
        self.user_pool.submit_delay = 2
        with self.assertRaises(ConnectionError):
            await miner.submit()
        await asyncio.wait_for(miner.closed.wait(),2)
        for _ in range(100):
            metrics = await self.api('/api/metrics')
            if metrics['active'] == 0:
                break
            await asyncio.sleep(.02)
        self.assertEqual(metrics['active'],0)
        self.assertEqual(metrics['zombie_reaped'],1)
        self.assertEqual(metrics['unmatched'],1)
        self.assertFalse((await self.api('/api/snapshot'))['sessions'])

    async def test_glued_handshake_objects_without_newlines(self):
        await self.launch(fee=False)
        reader, writer = await asyncio.open_connection('127.0.0.1', self.port)
        packets = [
            {'id':1,'method':'mining.subscribe','params':[]},
            {'id':2,'method':'mining.extranonce.subscribe','params':[]},
            {'id':3,'method':'mining.authorize','params':['user.worker','x']}]
        try:
            wire = b''.join(encode(p).rstrip(b'\n') for p in packets)
            # Split inside field names and string literals as a mobile link may.
            for i in range(0,len(wire),7):
                writer.write(wire[i:i+7])
                await writer.drain()
            replies = {}
            while len(replies) < 3:
                packet = json.loads(await asyncio.wait_for(reader.readline(),3))
                if packet.get('id') in (1,2,3):
                    replies[packet['id']] = packet
            self.assertIs(replies[2]['result'], True)
            self.assertIs(replies[3]['result'], True)
            self.assertEqual(self.user_pool.connections, 1)
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_unauthenticated_client_cannot_trigger_global_fee_watchdog(self):
        await self.launch(require_fee=True, handshake_timeout=5)
        miner = await self.miner()
        reader, writer = await asyncio.open_connection('127.0.0.1', self.port)
        try:
            writer.write(encode({'id':1,'method':'mining.subscribe','params':[]}))
            await writer.drain()
            await asyncio.wait_for(reader.readline(), 2)
            await asyncio.sleep(2.3)  # Longer than the configured fee grace.
            self.assertFalse((await self.api('/api/status'))['fee_policy_blocked'])
            self.assertFalse(miner.closed.is_set())
            self.assertTrue((await miner.request('mining.ping', []))['result'])
        finally:
            writer.close()
            await writer.wait_closed()

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
        snapshot = await self.api("/api/snapshot")
        worker = snapshot["registry"]["workers"][0]
        self.assertEqual((worker["worker"], worker["ip"], worker["accepted"]), ("user.worker", "127.0.0.1", 1000))
        self.assertEqual(worker["rejected"], 0)
        self.assertEqual(worker["stale"], 0)
        self.assertIsNone(worker["asic_ping_ms"])
        self.assertIsNotNone(worker["pool_response_ms"])
        self.assertGreater(worker["hashrate_windows"]["15m"]["hashrate_hs"], 0)
        self.assertTrue(any(item["switched_to_devfee"] for item in snapshot["events"]))
        self.assertNotIn("protocol_observation", snapshot["sessions"][0])

    async def test_two_fee_windows_run_concurrently_against_one_reserved_budget(self):
        await self.launch(fee_min_shares=197.5)
        first,second=await self.miner(),await self.miner()
        for _ in range(198):
            self.assertTrue((await first.submit())['result'])
        deadline=time.monotonic()+3
        while time.monotonic()<deadline and not (first.extra.startswith('2222') and second.extra.startswith('2222')):
            await asyncio.sleep(.02)
        self.assertTrue(first.extra.startswith('2222'))
        self.assertTrue(second.extra.startswith('2222'))
        self.assertNotEqual(first.extra,second.extra)
        status=await self.api('/api/status')
        self.assertEqual(status['fee_allocation']['active_grants'],2)
        results=await asyncio.gather(first.submit(),second.submit())
        self.assertTrue(all(result['result'] for result in results))
        self.assertEqual((self.user_pool.accepted,self.fee_pool.accepted),(198,2))
        self.assertEqual((self.user_pool.rejected,self.fee_pool.rejected),(0,0))
        self.assertFalse(first.closed.is_set() or second.closed.is_set())
        report=await self.api('/api/audit-export')
        self.assertAlmostEqual(report['fee_percentage'],1.0)
        status=await self.api('/api/status')
        self.assertEqual(status['fee_allocation']['pending_submissions'],0)
        self.assertEqual(status['fee_allocation']['reserved_difficulty'],'0')

    async def test_explicit_fee_worker_template_has_distinct_live_upstream_sessions(self):
        await self.launch(fee_template='{worker}_{slot}')
        miners=[await self.miner() for _ in range(3)]
        self.assertEqual(set(self.fee_pool.authorize_requests),{'fee.worker_1','fee.worker_2','fee.worker_3'})
        self.assertEqual(self.fee_pool.connections,3)
        self.assertTrue((await miners[0].submit())['result'])
        deadline=time.monotonic()+3
        while time.monotonic()<deadline and not any(miner.extra.startswith('2222') for miner in miners):
            await asyncio.sleep(.02)
        selected=next(miner for miner in miners if miner.extra.startswith('2222'))
        self.assertTrue((await selected.submit())['result'])
        self.assertEqual(self.fee_pool.accepted,1)
        self.assertEqual(self.fee_pool.rejected,0)
        self.assertTrue(all(not miner.closed.is_set() for miner in miners))

    async def test_fee_four_times_harder_is_charged_by_accepted_work(self):
        await self.launch(fee_difficulty_multiplier=4)
        miner=await self.miner()
        for _ in range(397):
            self.assertTrue((await miner.submit())['result'])
        self.assertEqual((self.user_pool.accepted,self.fee_pool.accepted),(396,1))
        self.assertEqual((self.user_pool.rejected,self.fee_pool.rejected),(0,0))
        report=await self.api('/api/audit-export')
        self.assertAlmostEqual(report['fee_percentage'],1.0)
        self.assertEqual(Fraction(report['fee_difficulty'])*99,
                         Fraction(report['accepted_difficulty']))
        self.assertFalse(miner.closed.is_set() or miner.reconnect.is_set())

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

    async def test_live_worker_stream_updates_and_survives_miner_reconnect(self):
        await self.launch(fee=False)
        miner = await self.miner()
        reader, writer = await asyncio.open_connection("127.0.0.1", self.dashboard)
        try:
            writer.write(f"GET /api/stream HTTP/1.1\r\nHost: 127.0.0.1:{self.dashboard}\r\n\r\n".encode())
            await writer.drain()
            headers = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            self.assertIn(b"text/event-stream", headers)
            first = await asyncio.wait_for(reader.readuntil(b"\n\n"), 5)
            state = json.loads(first.split(b"data: ")[1])
            worker_id = state["registry"]["workers"][0]["id"]
            self.assertTrue((await miner.submit())["result"])
            for _ in range(10):
                frame = await asyncio.wait_for(reader.readuntil(b"\n\n"), 5)
                state = json.loads(frame.split(b"data: ")[1])
                if state["registry"]["workers"][0]["accepted"] == 1:
                    break
            else:
                self.fail("SSE did not publish the accepted share")
            self.assertEqual(state["registry"]["workers"][0]["id"], worker_id)
            self.assertTrue((await miner.submit())["result"])
            measured = (await self.api("/api/workers"))["workers"][0]
            self.assertEqual(measured["notify_to_first_share_samples"], 1)
            self.assertGreaterEqual(measured["notify_to_first_share_ema_ms"], 0)
            await miner.close()
            for _ in range(10):
                frame = await asyncio.wait_for(reader.readuntil(b"\n\n"), 5)
                state = json.loads(frame.split(b"data: ")[1])
                if state["registry"]["workers"][0]["status"] == "offline":
                    break
            else:
                self.fail("Worker disconnect was not published")
            replacement = await self.miner()
            self.assertTrue((await replacement.submit())["result"])
            row = (await self.api("/api/workers"))["workers"][0]
            self.assertEqual((row["id"], row["accepted"], row["status"]), (worker_id, 3, "online"))
            self.assertEqual(row["notify_to_first_share_samples"], 2)
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_route_reload_freezes_old_credentials_and_reconnects_to_new_pools(self):
        await self.launch(fee=False, admin_token="A" * 32)
        old = await self.miner()
        self.assertTrue((await old.submit())["result"])
        new_user = await self.pool("33333333", "custom.wallet")
        new_fee = await self.pool("44444444", "new.fee")
        config = {"user_pool": {"host": "127.0.0.1", "port": new_user.port, "worker": "custom.wallet", "password": "private-password"},
                  "reconnect": False}
        result = await self.api("/api/config", config)
        self.assertNotIn("private-password", json.dumps(result))
        self.assertTrue((await old.submit())["result"])
        self.assertFalse(old.closed.is_set())
        fresh = await self.miner()
        self.assertTrue((await fresh.submit())["result"])
        self.assertEqual((self.user_pool.accepted, new_user.accepted), (2, 1))
        result = await self.api("/api/config", {"devfee_pool": {"host": "127.0.0.1", "port": new_fee.port,
                                                              "worker": "new.fee", "password": "fee-secret"}})
        await asyncio.wait_for(asyncio.gather(old.closed.wait(), fresh.closed.wait()), 5)
        self.assertTrue(old.reconnect.is_set())
        self.assertTrue(fresh.reconnect.is_set())
        replacement = await self.miner()
        self.assertTrue((await replacement.submit())["result"])
        self.assertTrue((await replacement.submit())["result"])
        self.assertEqual(new_fee.accepted, 1)
        self.assertEqual((new_fee.rejected, new_user.rejected), (0, 0))
        config = await self.api("/api/config")
        self.assertEqual(config["devfee_pool"]["worker"], "new.fee")
        self.assertNotIn("fee-secret", json.dumps(config))

    async def test_unreachable_route_does_not_commit_or_disconnect(self):
        await self.launch(fee=False, admin_token="A" * 32)
        miner = await self.miner()
        generation = (await self.api("/api/config"))["generation"]
        port = unused_ports(1)[0]
        await self.api("/api/config", {"user_pool": {"host": "127.0.0.1", "port": port}}, expected=503)
        config = await self.api("/api/config")
        self.assertEqual(config["generation"], generation)
        self.assertEqual(config["user_pool"]["port"], self.user_pool.port)
        self.assertFalse(miner.closed.is_set())
        self.assertTrue((await miner.submit())["result"])

    async def _shutdown_history(self, signal_mode=False):
        from types import SimpleNamespace
        from proxy import run, run_cli
        from observer import WorkerRegistry
        from history_store import HistoryStore
        pool = await self.pool("11111111", "user.worker")
        self.port, self.dashboard = unused_ports(2)
        self.admin_token = os.environ.get("CHEFAS_PROXY_ADMIN_TOKEN")
        path = Path(self.history.name) / "shutdown-history.json"
        args = SimpleNamespace(pool_host="127.0.0.1", pool_port=pool.port, tls=False,
            backup_host=None, backup_port=None, max_connections=10, connect_timeout=1,
            allow_cidr=[], idle_timeout=0, active_router=True, validate_pow=True,
            devfee_host=None, devfee_port=None, devfee_worker=None, devfee_password="x",
            reconnect_min_seconds=0, require_devfee=False, fee_grace_seconds=180,
            fee_min_work=0, history_file=str(path), control_state=str(Path(self.history.name)/"shutdown-control.json"),
            listen_host="127.0.0.1", listen_port=self.port, dashboard_port=self.dashboard)
        running = asyncio.create_task(run_cli(args) if signal_mode else run(args))
        try:
            for _ in range(100):
                try:
                    await self.api("/api/status")
                    break
                except OSError:
                    await asyncio.sleep(.02)
            miner = await self.miner()
            self.assertTrue((await miner.submit())["result"])
            if signal_mode:
                import signal
                signal.raise_signal(signal.SIGTERM)
                await asyncio.wait_for(running, 5)
                await asyncio.wait_for(miner.closed.wait(), 2)
                self.assertTrue(miner.reconnect.is_set())
            else:
                running.cancel()
                # Simulate Python 3.10 runner cancelling background jobs as well.
                for task in asyncio.all_tasks():
                    if task.get_coro().__qualname__ == "HistoryStore._run":
                        task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await running
            restored = WorkerRegistry()
            HistoryStore(restored, path).load()
            row = restored.snapshot()["workers"][0]
            self.assertEqual((row["accepted"], row["status"]), (1, "offline"))
            self.assertEqual(row["notify_to_first_share_samples"], 1)
        finally:
            if not running.done():
                running.cancel()
            await asyncio.gather(running, return_exceptions=True)

    async def test_graceful_proxy_shutdown_saves_worker_history(self):
        await self._shutdown_history()

    async def test_sigterm_closes_live_miner_and_saves_history(self):
        await self._shutdown_history(signal_mode=True)

    async def test_debug_harness_traces_jobs_without_authorization_secrets(self):
        await self.launch(fee=False, debug=True, admin_token="A"*32)
        secret = "fixture-asic-private-password"
        miner = await self.miner(password=secret)
        malformed = miner.share_params()
        malformed.append(secret)  # broken firmware copies password to version bits
        self.assertFalse((await miner.submit(malformed))["result"])
        self.assertTrue((await miner.submit())["result"])
        path = Path(self.history.name) / "logs" / "proxy.log"
        for _ in range(100):
            text = path.read_text(encoding="utf-8")
            if "proxy_to_upstream" in text and "share_rejected_local" in text:
                break
            await asyncio.sleep(.02)
        else:
            self.fail("Pilot packet events were not written")
        self.assertNotIn(secret, text)
        self.assertNotIn("A"*32, text)
        self.assertNotIn("mining.authorize", text)
        self.assertIn("mining.notify", text)
        self.assertIn("mining.submit", text)
        self.assertIn("[REDACTED]", text)


if __name__ == "__main__":
    unittest.main()
