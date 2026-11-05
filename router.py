"""Transparent stream forwarding with passive observation and backpressure."""


async def copy_stream(source, destination, direction, observer, metrics, on_activity):
    while data := await source.read(65536):
        on_activity()
        destination.write(data)
        try:
            observer.feed(direction, data)
        except Exception:
            metrics.observation_errors += 1
        await destination.drain()


import asyncio
import string
import time
from collections import OrderedDict

from upstream_manager import UpstreamConnection, ProtocolError, read_packet, encode_packet
from pow_validator import (validate_stratum_share, target_from_difficulty,
                           build_stratum_header, compact_target, meets_target)


class StatefulRouter:
    """Experimental per-miner router. Jobs retain their issuing connection."""

    MAX_JOBS = 128
    MAX_DUPLICATES = 8192

    def __init__(self, relay, reader, writer, session):
        self.relay, self.reader, self.writer, self.session = relay, reader, writer, session
        self.channels = {}
        self.current = None
        self.config = self.subscription = None
        self.mask = None
        self.auth = {}
        self.hot = False
        self.jobs, self.duplicates = OrderedDict(), OrderedDict()
        self.sequence = 0
        self.write_lock = asyncio.Lock()
        self.switch_lock = asyncio.Lock()
        self.tasks = set()
        self.stopping = False
        self.started = time.monotonic()
        self.prefer_fee = bool(relay.fee and relay.ledger.should_fee() and relay.fee_owner is None)
        if self.prefer_fee:
            relay.fee_owner = self

    async def send(self, packet):
        async with self.write_lock:
            self.writer.write(encode_packet(packet))
            await asyncio.wait_for(self.writer.drain(), self.relay.timeout)

    async def connect(self, name, endpoint):
        if await self.relay.is_self_route(endpoint):
            self.relay.metrics.blocked_loops += 1
            raise ConnectionError("upstream loops back to listener")
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(*endpoint, ssl=self.relay.tls_context), self.relay.timeout)
        ch = UpstreamConnection(reader, writer, name, self.notification, self.disconnected, self.relay.timeout)
        self.channels[name] = ch
        return ch

    @staticmethod
    def subscription_result(ch, result):
        if (not isinstance(result, list) or len(result) < 3 or
            not isinstance(result[1], str) or len(result[1]) > 64 or len(result[1]) % 2 or
            any(c not in string.hexdigits for c in result[1]) or
            type(result[2]) is not int or not 0 <= result[2] <= 32):
            raise ProtocolError("invalid subscription")
        try:
            bytes.fromhex(result[1])
        except ValueError as exc:
            raise ProtocolError("invalid extranonce") from exc
        ch.extranonce1, ch.extranonce2_size, ch.subscribed = result[1], result[2], True

    async def prepare(self, name, endpoint, fee=False):
        ch = await self.connect(name, endpoint)
        if self.config is not None:
            response = await ch.request("mining.configure", self.config)
            if self.mask is not None:
                result = response.get("result")
                try:
                    compatible = (isinstance(result, dict) and result.get("version-rolling") is True
                                  and int(self.mask, 16) & ~int(result["version-rolling.mask"], 16) == 0)
                except (KeyError, TypeError, ValueError):
                    compatible = False
                if not compatible:
                    raise ProtocolError("incompatible version rolling mask")
        if self.subscription is not None:
            response = await ch.request("mining.subscribe", self.subscription)
            self.subscription_result(ch, response.get("result"))
            ch.subscription_result = response["result"]
        for worker, password in self.auth.items():
            response = await ch.request("mining.authorize", [self.relay.fee_worker if fee else worker,
                                        self.relay.fee_password if fee else password])
            if response.get("result") is not True or response.get("error") is not None:
                raise ProtocolError("upstream authorization failed")
            ch.authorized = True
        self.update_ready(ch)
        return ch

    @staticmethod
    def update_ready(ch):
        if (ch.subscribed and ch.authorized and ch.job is not None and
                getattr(ch, "job_difficulty", None) is not None):
            ch.ready.set()

    async def notification(self, ch, msg):
        method, params = msg.get("method"), msg.get("params")
        if not isinstance(params, list):
            raise ProtocolError("invalid notification")
        if method == "mining.set_difficulty":
            if len(params) != 1:
                raise ProtocolError("invalid difficulty")
            target_from_difficulty(params[0])
            ch.difficulty = params[0]
            if self.current is ch:
                self.session["observer"].protocol["observed_difficulty"] = float(params[0])
        elif method == "mining.notify":
            if len(params) != 9 or not isinstance(params[0], str) or type(params[8]) is not bool:
                raise ProtocolError("invalid job")
            ch.job = list(params)
            ch.job_difficulty = ch.difficulty
            ch.job_extranonce = ch.extranonce1
            ch.job_extra_size = ch.extranonce2_size
        elif method == "mining.set_extranonce":
            self.subscription_result(ch, [None] + params)
            ch.job = None
            ch.ready.clear()
            if self.current is ch:
                self.session["observer"].protocol.update(extranonce1=ch.extranonce1,
                                                         extranonce2_size=ch.extranonce2_size)
        elif method == "mining.set_version_mask":
            # Dynamic masks need renegotiation with every upstream; never send
            # shares under a mask that another connection has not accepted.
            self.writer.close()
            return
        elif method == "client.reconnect":
            self.session["metrics"].redirect_notices += 1
        self.update_ready(ch)
        if self.current is not ch or not ch.authorized:
            return
        if method == "mining.notify" and ch.difficulty is not None:
            await self.publish(ch, False)
        elif method == "mining.set_extranonce" and not self.hot:
            self.writer.close()
        elif method != "mining.notify":
            await self.send(msg)

    def make_job(self, ch, clean):
        params = list(ch.job)
        self.sequence += 1
        local_id = format(self.sequence, "x")
        upstream_id = params[0]
        params[0] = local_id
        if clean:
            params[8] = True
        self.jobs[local_id] = (ch, upstream_id, list(ch.job), ch.job_extranonce,
                               ch.job_extra_size, ch.job_difficulty, self.mask)
        while len(self.jobs) > self.MAX_JOBS:
            self.jobs.popitem(last=False)
        return {"id": None, "method": "mining.notify", "params": params}

    async def publish(self, ch, clean):
        async with self.write_lock:
            self.writer.write(encode_packet(self.make_job(ch, clean)))
            await asyncio.wait_for(self.writer.drain(), self.relay.timeout)

    async def activate(self, ch):
        async with self.switch_lock:
            if ch.closed or not ch.ready.is_set() or ch.job is None or ch.job_difficulty is None:
                return False
            async with self.write_lock:
                self.current = ch
                self.session["pool"] = "primary" if ch.target == "user" else ch.target
                self.session["observer"].protocol.update(extranonce1=ch.extranonce1,
                    extranonce2_size=ch.job_extra_size, observed_difficulty=float(ch.job_difficulty))
                for packet in (
                    {"id": None, "method": "mining.set_extranonce", "params": [ch.job_extranonce, ch.job_extra_size]},
                    {"id": None, "method": "mining.set_difficulty", "params": [ch.job_difficulty]},
                    self.make_job(ch, True),
                ):
                    self.writer.write(encode_packet(packet))
                await asyncio.wait_for(self.writer.drain(), self.relay.timeout)
            return True

    async def disconnected(self, ch):
        if self.stopping:
            return
        # Losing the user channel while mining a fee window must also trigger
        # recovery; otherwise the miner could remain on the fee indefinitely.
        if self.current is not ch and not (ch.target in ("user", "backup") and
                                            self.current and self.current.target == "fee"):
            return
        task = asyncio.create_task(self.recover(ch))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def recover(self, ch):
        try:
            if ch.target == "fee":
                target = next((item for name in ("user", "backup")
                               if (item := self.channels.get(name)) and not item.closed), None)
            elif self.relay.backup and ch.target != "backup":
                target = await self.prepare("backup", self.relay.backup)
                await asyncio.wait_for(target.ready.wait(), self.relay.timeout)
            else:
                target = None
            if target is None or target.closed or not self.hot or not await self.activate(target):
                self.writer.close()
        except (OSError, ValueError, ProtocolError, asyncio.TimeoutError):
            self.writer.close()
        finally:
            if self.relay.fee_owner is self and (ch.target == "fee" or
                                                  self.current is not None and self.current.target != "fee"):
                self.relay.fee_owner = None

    async def reconcile(self):
        if not self.relay.fee or self.current is None:
            return
        fee = self.channels.get("fee")
        user = next((item for name in ("user", "backup")
                     if (item := self.channels.get(name)) and not item.closed), None)
        available = bool(fee and not fee.closed and fee.ready.is_set())
        want = self.relay.ledger.should_fee(fee_available=available)
        if self.current.target == "fee" and not want:
            if self.hot and user and not user.closed:
                await self.activate(user)
                if self.relay.fee_owner is self:
                    self.relay.fee_owner = None
            elif not self.hot:
                await self.send({"id": None, "method": "client.reconnect", "params": []})
                self.writer.close()
            else:
                # Fail closed rather than mine indefinitely to the fee account.
                self.writer.close()
        elif self.current.target != "fee" and want and self.relay.fee_owner in (None, self):
            if self.hot:
                self.relay.fee_owner = self
                await self.activate(fee)
            elif time.monotonic() - self.started >= self.relay.reconnect_min_seconds:
                await self.send({"id": None, "method": "client.reconnect", "params": []})
                self.writer.close()

    async def reply(self, ident, response):
        await self.send({"id": ident, "result": response.get("result"), "error": response.get("error")})

    async def submit(self, ident, params):
        metrics = self.session["metrics"]
        metrics.submitted += 1
        category = "malformed_local"
        try:
            if not isinstance(params, list) or len(params) not in (5, 6) or not all(isinstance(x, str) for x in params):
                raise ValueError("malformed share")
            if params[0] not in self.auth or params[1] not in self.jobs:
                category = "unknown_job_local"
                raise ValueError("unknown worker or job")
            ch, upstream_id, job, extra, size, difficulty, mask = self.jobs[params[1]]
            if ch.closed or ch.extranonce1 != extra or ch.extranonce2_size != size:
                category = "unknown_job_local"
                raise ValueError("job session is no longer valid")
            key = (id(ch), upstream_id, extra, *params[2:])
            if key in self.duplicates:
                category = "duplicate_local"
                raise ValueError("duplicate")
            if self.relay.validate_pow:
                valid = validate_stratum_share(job, extra, params[2], params[3], params[4],
                    params[5] if len(params) == 6 else None, mask, difficulty, extranonce2_size=size)
                if not valid:
                    # Never suppress a potential network block when a pool has
                    # assigned difficulty above the network target.
                    header = build_stratum_header(job, extra, params[2], params[3], params[4],
                        params[5] if len(params) == 6 else None, mask)
                    if not meets_target(header, compact_target(int(job[6], 16))):
                        category = "low_diff_local"
                        raise ValueError("low difficulty")
            self.duplicates[key] = None
            while len(self.duplicates) > self.MAX_DUPLICATES:
                self.duplicates.popitem(last=False)
        except (ValueError, TypeError):
            metrics.invalid_local += 1
            setattr(metrics, category, getattr(metrics, category) + 1)
            await self.reply(ident, {"result": False, "error": [23, "Invalid or unknown share", None]})
            return
        outbound = list(params)
        outbound[1] = upstream_id
        if ch.target == "fee":
            outbound[0] = self.relay.fee_worker
        fee = self.channels.get("fee")
        fee_available = bool(fee and not fee.closed and fee.ready.is_set())
        try:
            self.session["pending"] = self.session.get("pending", 0) + 1
            response = await ch.request("mining.submit", outbound)
            if response.get("result") is True and response.get("error") is None:
                metrics.accepted += 1
                self.relay.ledger.record("fee" if ch.target == "fee" else "user", difficulty,
                                         fee_available=fee_available)
                self.relay.metrics._audit.record("fee" if ch.target == "fee" else "user", difficulty)
            elif response.get("result") is False or response.get("error") is not None:
                metrics.rejected += 1
            else:
                metrics.unmatched += 1
            await self.reply(ident, response)
            await self.reconcile()
        except (ConnectionError, asyncio.TimeoutError):
            metrics.unmatched += 1
            await self.reply(ident, {"result": None, "error": [20, "Upstream unavailable", None]})
        finally:
            self.session["pending"] -= 1

    async def run(self):
        try:
            try:
                user = await self.connect("user", (self.relay.host, self.relay.port))
            except (OSError, asyncio.TimeoutError):
                self.relay.metrics.upstream_failures += 1
                if not self.relay.backup:
                    raise
                user = await self.connect("backup", self.relay.backup)
                self.relay.metrics.backup_connections += 1
            self.current = user
            self.session["state"], self.session["pool"] = "connected", "primary" if user.target == "user" else user.target
            while packet := await read_packet(self.reader):
                ident, method, params = packet.get("id"), packet.get("method"), packet.get("params", [])
                if (not isinstance(ident, (int, str)) or isinstance(ident, bool) or
                    not isinstance(method, str) or not isinstance(params, list)):
                    raise ProtocolError("invalid miner request")
                if (isinstance(ident, str) and len(ident) > 128) or (isinstance(ident, int) and ident.bit_length() > 256):
                    raise ProtocolError("request identifier too large")
                if method == "mining.configure":
                    self.config = params
                    response = await user.request(method, params)
                    result = response.get("result")
                    if isinstance(result, dict) and result.get("version-rolling") is True:
                        self.mask = result.get("version-rolling.mask")
                        try:
                            if not isinstance(self.mask, str) or len(self.mask) != 8:
                                raise ValueError("invalid mask")
                            mask_value = int(self.mask, 16)
                            requested = params[1].get("version-rolling.mask", "ffffffff")
                            if not isinstance(requested, str) or len(requested) != 8 or mask_value & ~int(requested, 16):
                                raise ValueError("mask exceeds request")
                        except (IndexError, AttributeError, TypeError, ValueError) as exc:
                            raise ProtocolError("invalid version rolling mask") from exc
                    fee = self.channels.get("fee")
                    if fee is not None:
                        try:
                            fee_result = (await fee.request(method, params)).get("result")
                            compatible = (self.mask is None or
                                (isinstance(fee_result, dict) and fee_result.get("version-rolling") is True and
                                 int(self.mask, 16) & ~int(fee_result["version-rolling.mask"], 16) == 0))
                        except (KeyError, TypeError, ValueError, OSError, asyncio.TimeoutError):
                            compatible = False
                        if not compatible:
                            if self.current is fee:
                                raise ProtocolError("incompatible active mask")
                            self.channels.pop("fee", None)
                            await fee.close()
                    if isinstance(result, dict) and response.get("error") is None:
                        self.session["observer"].protocol.update(configured=True,
                            version_rolling=result.get("version-rolling"), version_mask=self.mask)
                    await self.reply(ident, response)
                elif method == "mining.subscribe":
                    self.subscription = params
                    response = await user.request(method, params)
                    self.subscription_result(user, response.get("result"))
                    if self.relay.fee:
                        try:
                            fee = await self.prepare("fee", self.relay.fee, fee=True)
                            if self.prefer_fee:
                                self.current = fee
                                response = dict(response, result=fee.subscription_result)
                        except (OSError, ValueError, ProtocolError, asyncio.TimeoutError):
                            fee = self.channels.pop("fee", None)
                            if fee:
                                await fee.close()
                            if self.relay.fee_owner is self:
                                self.relay.fee_owner = None
                    self.session["observer"].protocol.update(subscribed=True,
                        extranonce1=self.current.extranonce1, extranonce2_size=self.current.extranonce2_size)
                    await self.reply(ident, response)
                elif method == "mining.extranonce.subscribe":
                    self.hot = True
                    self.session["observer"].protocol["extranonce_subscribed"] = True
                    await self.reply(ident, {"result": True, "error": None})
                elif method == "mining.authorize":
                    response = await user.request(method, params)
                    accepted = response.get("result") is True and response.get("error") is None
                    if accepted and len(params) >= 2 and all(isinstance(x, str) for x in params[:2]):
                        if len(self.auth) >= 32 and params[0] not in self.auth:
                            raise ProtocolError("too many workers")
                        if len(params[0]) > 128 or len(params[1]) > 1024:
                            raise ProtocolError("credentials too long")
                        self.auth[params[0]] = params[1]
                        user.authorized = True
                        self.session["worker"] = "".join(c if c.isprintable() else "?" for c in params[0][:128])
                        self.session["observer"].protocol["authorized_workers"] = list(self.auth)
                        fee = self.channels.get("fee")
                        if fee:
                            try:
                                fee_response = await fee.request(method, [self.relay.fee_worker, self.relay.fee_password])
                                fee.authorized = fee_response.get("result") is True and fee_response.get("error") is None
                            except (OSError, asyncio.TimeoutError):
                                fee.authorized = False
                            if not fee.authorized and self.current is fee:
                                raise ProtocolError("fee authorization failed")
                    await self.reply(ident, response)
                    for ch in self.channels.values():
                        self.update_ready(ch)
                    if self.current.job is not None and getattr(self.current, "job_difficulty", None) is not None:
                        await self.send({"id": None, "method": "mining.set_difficulty", "params": [self.current.job_difficulty]})
                        await self.publish(self.current, False)
                elif method == "mining.submit":
                    await self.submit(ident, params)
                else:
                    await self.reply(ident, await self.current.request(method, params))
        except (OSError, ValueError, TypeError, ProtocolError, asyncio.TimeoutError):
            self.relay.metrics.transport_errors += 1
        finally:
            self.stopping = True
            if self.relay.fee_owner is self:
                self.relay.fee_owner = None
            for task in list(self.tasks):
                task.cancel()
            if self.tasks:
                await asyncio.gather(*self.tasks, return_exceptions=True)
            for ch in list(self.channels.values()):
                await ch.close()
