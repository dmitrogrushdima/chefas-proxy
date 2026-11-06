"""Transparent stream forwarding with passive observation and backpressure."""


async def copy_stream(source, destination, direction, observer, metrics, on_activity, timeout=10, source_writer=None):
    while data := await source.read(65536):
        on_activity()
        write_bounded(destination,data,metrics)
        try:
            if observer is not None:
                observer.feed(direction, data)
        except Exception:
            metrics.observation_errors += 1
        await drain_bounded(destination,timeout,source_writer,source,metrics)


import asyncio
import string
import time
import logging
import ssl
from collections import OrderedDict, deque

from upstream_manager import UpstreamConnection, ProtocolError, encode_packet, open_upstream, UpstreamLoopError
from pow_validator import (validate_stratum_share, target_from_difficulty,
                           build_stratum_header, compact_target, meets_target)
from devfee_scheduler import (FeeHealth, FeeJitter, independent_subscription, FairnessAllocator,
                              stable_fee_worker)
from network_io import write_bounded, drain_bounded
from pilot_logging import event, stratum_trace, remember_secret
from stratum_codec import PacketReader
from hardware_compatibility import (ClientCapabilities, validate_config, negotiated_mask,
                                    upstream_config, mask_value, merge_config)


class StatefulRouter:
    """Experimental per-miner router. Jobs retain their issuing connection."""

    MAX_JOBS = 128
    MAX_DUPLICATES = 8192

    def __init__(self, relay, reader, writer, session):
        self.relay, self.reader, self.writer, self.session = relay, reader, writer, session
        self.packets = PacketReader(reader, getattr(relay, "max_miner_message", 4096), role="miner", metrics=relay.metrics)
        self.fee = relay.fee
        self.fee_worker = getattr(relay, 'fee_worker', '')
        self.fee_password = getattr(relay, 'fee_password', 'x')
        self.user_worker = getattr(relay, 'user_worker', '')
        self.user_password = getattr(relay, 'user_password', 'x')
        self.channels = {}
        self.current = None
        self.config = self.subscription = None
        self.mask = None
        self.auth = {}
        self.hot = False
        self.jobs, self.duplicates = OrderedDict(), OrderedDict()
        self.job_times = {}
        self.job_layouts = {}
        self.capabilities = ClientCapabilities(getattr(relay,'asic_extranonce2_min',0),
                                               getattr(relay,'asic_extranonce2_max',4))
        self.last_client_difficulty = None
        self.last_transition = None
        self.client_disconnected = False
        self.intentional_close = False
        self.sequence = 0
        self.write_lock = asyncio.Lock()
        self.switch_lock = asyncio.Lock()
        self.tasks = set()
        self.stopping = False
        self.retired = []
        self.route_updating = False
        self.logged_channel = None
        self.started = time.monotonic()
        self.last_miner_activity = self.started
        self.owner = None
        self.fee_policy_started = False
        session["router"] = self
        self.health = FeeHealth(getattr(relay, "fee_grace_seconds", 180))
        self.prefer_fee = False
        self.jitter = FeeJitter(getattr(relay, 'fee_jitter_seconds', 180))
        self.warmup_task = None
        self.clean_revision = 0
        self.urgent_job = self.normal_job = None
        self.urgent_inflight = None
        self.controls = deque()
        self.control_bytes = 0
        self.outbound_ready = asyncio.Event()
        self.client_context = None
        self.reconnect_task = None
        self.reconcile_task = None
        self.reconcile_lock = asyncio.Lock()
        if not hasattr(relay,'fee_allocator'):
            relay.fee_allocator = FairnessAllocator(relay.ledger)
        self.allocator = relay.fee_allocator
        self.fee_slot = None
        if self.fee and getattr(relay,'fee_worker_template',None):
            self.fee_slot = relay.fee_slots.obtain(session['id'])
            try:
                self.fee_worker = stable_fee_worker(self.fee_worker,self.fee_slot,relay.fee_worker_template)
            except ValueError:
                relay.fee_slots.release(session['id'])
                raise

    def schedule_reconcile(self):
        if self.reconcile_task is None or self.reconcile_task.done():
            async def update():
                try:
                    await self.reconcile()
                except (OSError,ValueError,ProtocolError,asyncio.TimeoutError):
                    self.close_client()
            self.reconcile_task = asyncio.create_task(update())
            self.tasks.add(self.reconcile_task)
            self.reconcile_task.add_done_callback(self.tasks.discard)

    def queue_job(self, ch, clean):
        snapshot = (list(ch.job), ch.job_extranonce, ch.job_extra_size, ch.job_difficulty,
                    self.mask, getattr(ch,'clean_generation',0))
        item = (ch,snapshot)
        if clean:
            if self.urgent_job is not None or self.normal_job is not None:
                self.relay.metrics.clean_jobs_coalesced += 1
            self.urgent_job, self.normal_job = item, None
        else:
            self.normal_job = item
        self.outbound_ready.set()

    def queue_control(self, ch, packet):
        size = len(encode_packet(packet))
        if len(self.controls) >= 32 or self.control_bytes + size > 262144:
            raise ProtocolError('notification queue limit')
        self.controls.append((ch,packet,size))
        self.control_bytes += size
        self.outbound_ready.set()

    async def dispatch_notifications(self):
        try:
            while not self.stopping:
                await self.outbound_ready.wait()
                if self.urgent_job is not None:
                    ch,snapshot = self.urgent_job
                    self.urgent_job = None
                    self.urgent_inflight = (ch,snapshot)
                    try:
                        await self.publish(ch,True,snapshot)
                    finally:
                        self.urgent_inflight = None
                elif self.controls:
                    ch,packet,size = self.controls.popleft()
                    self.control_bytes -= size
                    if not await self.send_current(ch,packet,yield_to_clean=True):
                        self.controls.appendleft((ch,packet,size))
                        self.control_bytes += size
                elif self.normal_job is not None:
                    ch,snapshot = self.normal_job
                    self.normal_job = None
                    await self.publish(ch,False,snapshot)
                else:
                    self.outbound_ready.clear()
        except (OSError, asyncio.TimeoutError, ProtocolError, ValueError):
            self.close_client()

    def schedule_reconnect(self):
        if self.reconnect_task is None or self.reconnect_task.done():
            self.reconnect_task = asyncio.create_task(self.request_reconnect())
            self.tasks.add(self.reconnect_task)
            def finished(task):
                self.tasks.discard(task)
                if not task.cancelled(): task.exception()
            self.reconnect_task.add_done_callback(finished)

    def prune_jobs(self, now=None):
        now = time.monotonic() if now is None else now
        ttl = getattr(self.relay, 'job_ttl', 900)
        expired = [key for key, timing in self.job_times.items() if now - timing[0] >= ttl]
        for key in expired:
            self.jobs.pop(key, None)
            self.job_times.pop(key, None)
            self.job_layouts.pop(key, None)
        for key, (job_id, created) in list(self.duplicates.items()):
            if job_id not in self.jobs or now - created >= ttl:
                self.duplicates.pop(key, None)
        return len(expired)

    async def collect_jobs(self):
        while True:
            await asyncio.sleep(getattr(self.relay, 'job_gc_interval', 60))
            self.prune_jobs()

    def schedule_fee_warmup(self):
        if (self.fee and self.auth and self.subscription is not None and self.jitter.maximum and
                (self.warmup_task is None or self.warmup_task.done())):
            self.warmup_task = asyncio.create_task(self.warm_fee())
            self.tasks.add(self.warmup_task)
            self.warmup_task.add_done_callback(self.tasks.discard)

    async def warm_fee(self):
        while not self.stopping:
            ch = self.channels.get('fee')
            try:
                if ch is None or ch.closed:
                    ch = await self.prepare('fee', self.fee, fee=True)
                    await asyncio.wait_for(ch.ready.wait(), self.relay.timeout)
                    if ch.closed or not ch.authorized:
                        raise ProtocolError('fee warmup failed')
                    await self.reconcile()
                try:
                    await asyncio.shield(ch.task)
                except asyncio.CancelledError:
                    # Closing a failed upstream cancels its reader, not this
                    # supervisor. Session shutdown still cancels us normally.
                    if self.stopping or not ch.task.cancelled():
                        raise
            except (OSError, ValueError, ProtocolError, asyncio.TimeoutError):
                event(logging.WARNING, 'fee_warmup_failed', session_id=self.session['id'])
            ch = self.channels.get('fee')
            if ch is not None:
                await ch.close()
            self.jitter.defer()
            # Even a zero random draw must not create a connection retry loop.
            await asyncio.sleep(max(5, self.jitter.remaining()))

    @staticmethod
    def abort(writer):
        if writer is not None:
            writer.close()
            transport = getattr(writer, "transport", None)
            if transport is not None:
                transport.abort()

    def close_client(self):
        if not self.client_disconnected:
            self.intentional_close = True
        self.writer.close()

    def write_client(self, packet):
        try:
            write_bounded(self.writer,encode_packet(packet),self.relay.metrics)
        except (ConnectionResetError, BrokenPipeError):
            self.client_disconnected = True
            raise

    async def drain_client(self):
        try:
            await drain_bounded(self.writer,self.relay.timeout,metrics=self.relay.metrics)
        except (ConnectionResetError, BrokenPipeError):
            self.client_disconnected = True
            raise

    def can_hot_switch(self):
        return (self.hot and getattr(self.relay,'hot_switch_mode','auto') != 'reconnect' and
                not self.relay.compatibility.requires_reconnect(self.session.get('ip',''), self.auth,
                                                                self.capabilities.agent))

    async def request_reconnect(self):
        try:
            await self.send({'id':None,'method':'client.reconnect','params':[]})
        finally:
            self.close_client()

    def note_difficulty(self, difficulty):
        if self.auth and self.last_client_difficulty is not None and difficulty != self.last_client_difficulty:
            self.last_transition = (time.monotonic(), 'difficulty')
        self.last_client_difficulty = difficulty

    async def watch_miner(self):
        while True:
            if not self.auth:
                remaining = getattr(self.relay, "handshake_timeout", 30) - (time.monotonic() - self.started)
                counter = "handshake_timeouts"
            else:
                timeout = getattr(self.relay, "miner_timeout", 600)
                if not timeout:
                    return
                remaining = timeout - (time.monotonic() - self.last_miner_activity)
                counter = "zombie_reaped"
            if remaining <= 0:
                setattr(self.relay.metrics, counter, getattr(self.relay.metrics, counter) + 1)
                self.intentional_close = True
                self.abort(self.writer)
                for channel in list(self.channels.values()):
                    self.abort(channel.writer)
                self.owner.cancel()
                return
            await asyncio.sleep(min(remaining, .25))

    def start_fee_policy(self):
        if getattr(self.relay, "require_devfee", False) and not self.fee_policy_started:
            self.health = FeeHealth(getattr(self.relay, "fee_grace_seconds", 180))
            self.fee_policy_started = True
            self.tasks.add(asyncio.create_task(self.watch_fee()))

    async def miner_packets(self, first):
        yield first
        try:
            while (packet := await self.packets.read_packet()) is not None:
                yield packet
            self.client_disconnected = True
        except (ConnectionResetError, BrokenPipeError):
            self.client_disconnected = True
            raise

    async def send(self, packet):
        async with self.write_lock:
            self.write_client(packet)
            await self.drain_client()

    async def send_current(self, channel, packet, yield_to_clean=False):
        async with self.write_lock:
            if yield_to_clean and self.urgent_job is not None:
                return False
            if self.current is channel and not channel.closed:
                if packet.get('method') == 'mining.set_difficulty':
                    self.note_difficulty(packet['params'][0])
                elif packet.get('method') == 'mining.set_extranonce':
                    self.last_transition = (time.monotonic(), 'extranonce')
                self.write_client(packet)
                await self.drain_client()
            return True

    def check_fee_policy(self):
        if not getattr(self.relay, "require_devfee", False):
            return
        if getattr(self.relay, "fee_blocked", False) or self.fee_policy_started and self.health.expired():
            if not getattr(self.relay, "fee_blocked", False):
                event(logging.WARNING, "required_fee_blocked", session_id=self.session["id"])
            self.relay.fee_blocked = True
            for session in self.relay.sessions.values():
                router = session.get("router")
                if router:
                    router.close_client()
            raise ProtocolError("required DevFee health expired")

    async def watch_fee(self):
        while True:
            self.check_fee_policy()
            fee = self.channels.get("fee")
            self.health.observe(bool(fee and not fee.closed and fee.authorized and fee.ready.is_set()))
            self.check_fee_policy()
            await asyncio.sleep(min(.25, self.health.grace_seconds / 4))

    async def connect(self, name, endpoint):
        if await self.relay.is_self_route(endpoint):
            self.relay.metrics.blocked_loops += 1
            raise ConnectionError("upstream loops back to listener")
        try:
            reader, writer = await open_upstream(endpoint, self.relay.tls_context, self.relay.timeout,
                                                 getattr(self.relay, 'url_tls_context', None),self.relay.resolver,
                                                 address_guard=self.relay.is_self_route)
        except UpstreamLoopError:
            self.relay.metrics.blocked_loops += 1
            raise
        except ssl.SSLCertVerificationError as exc:
            event(logging.WARNING, 'upstream_tls_verification_failed', target=name,
                  verify_code=exc.verify_code, session_id=self.session['id'])
            raise
        ch = UpstreamConnection(reader,writer,name,self.notification,self.disconnected,self.relay.timeout,
                                self.writer,self.reader,self.relay.metrics)
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
        if fee:
            await self.relay.fee_connection_gate.wait(0 if self.prefer_fee else self.jitter.remaining())
            if self.auth:
                self.start_fee_policy()
        ch = await self.connect(name, endpoint)
        if self.config is not None:
            params = upstream_config(self.config, self.mask)
            response = await ch.request("mining.configure", params)
            if self.mask is not None:
                if negotiated_mask(params, response) != self.mask:
                    raise ProtocolError("incompatible version rolling mask")
        if self.subscription is not None:
            response = await ch.request("mining.subscribe", independent_subscription(self.subscription))
            self.subscription_result(ch, response.get("result"))
            self.capabilities.layout(ch.extranonce1, ch.extranonce2_size)
            ch.subscription_result = response["result"]
        credentials = ([(self.fee_worker,self.fee_password)] if fee else
                       [(self.user_worker or worker,self.user_password if self.user_worker else password)
                        for worker,password in self.auth.items()])
        for worker,password in credentials:
            response = await ch.request('mining.authorize',[worker,password])
            if response.get("result") is not True or response.get("error") is not None:
                raise ProtocolError("upstream authorization failed")
            ch.authorized = True
        self.update_ready(ch)
        return ch

    @staticmethod
    def update_ready(ch):
        if (not ch.closed and ch.subscribed and ch.authorized and ch.job is not None and
                getattr(ch, "job_difficulty", None) is not None):
            ch.ready.set()
        else:
            ch.ready.clear()

    async def notification(self, ch, msg):
        self.check_fee_policy()
        if getattr(self.relay, "trace_stratum", False):
            stratum_trace("upstream_to_proxy", self.session["id"], ch.target, msg,
                          (self.user_password, self.fee_password, *self.auth.values()))
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
            if params[8]:
                ch.clean_generation = getattr(ch,'clean_generation',0) + 1
                if self.current is ch: self.clean_revision += 1
        elif method == "mining.set_extranonce":
            self.subscription_result(ch, [None] + params)
            self.capabilities.layout(ch.extranonce1, ch.extranonce2_size)
            ch.job = None
            ch.ready.clear()
            if self.current is ch:
                self.clean_revision += 1
                extra, size, _ = self.capabilities.layout(ch.extranonce1, ch.extranonce2_size)
                self.session["observer"].protocol.update(extranonce1=extra, extranonce2_size=size,
                    upstream_extranonce1=ch.extranonce1, upstream_extranonce2_size=ch.extranonce2_size)
        elif method == "mining.set_version_mask":
            if len(params) != 1:
                raise ProtocolError('invalid version mask notification')
            mask_value(params[0])
            if self.current is ch:
                self.schedule_reconnect()
            else:
                ch.ready.clear()
                ch.authorized = False
                await ch.close()
            return
        elif method == "client.reconnect":
            self.session["metrics"].redirect_notices += 1
        self.update_ready(ch)
        if self.current is not ch or not ch.authorized or not self.auth:
            return
        if method == "mining.notify" and ch.difficulty is not None:
            self.queue_job(ch,params[8])
        elif method == 'mining.set_difficulty':
            # Its exact threshold travels atomically with the next notify.
            return
        elif method == "mining.set_extranonce":
            if not self.can_hot_switch():
                self.schedule_reconnect()
        elif method != "mining.notify":
            self.queue_control(ch,msg)

    def make_job(self, ch, clean, snapshot=None):
        job,extra,size,difficulty,mask,_ = snapshot or (ch.job,ch.job_extranonce,ch.job_extra_size,ch.job_difficulty,self.mask,0)
        params = list(job)
        self.sequence += 1
        local_id = format(self.sequence, "x")
        upstream_id = params[0]
        params[0] = local_id
        if clean:
            params[8] = True
        self.jobs[local_id] = (ch,upstream_id,list(job),extra,size,difficulty,mask)
        self.job_times[local_id] = (time.monotonic(), set())
        self.job_layouts[local_id] = self.capabilities.layout(extra,size)
        while len(self.jobs) > self.MAX_JOBS:
            expired, _ = self.jobs.popitem(last=False)
            self.job_times.pop(expired, None)
            self.job_layouts.pop(expired, None)
        self.prune_jobs()
        return {"id": None, "method": "mining.notify", "params": params}

    async def publish(self, ch, clean, snapshot=None):
        self.check_fee_policy()
        async with self.write_lock:
            if self.current is not ch or ch.closed:
                return
            if snapshot is not None and (snapshot[5] != getattr(ch,'clean_generation',0) or
                    snapshot[1] != ch.extranonce1 or snapshot[2] != ch.extranonce2_size):
                return
            snapshot = snapshot or (list(ch.job),ch.job_extranonce,ch.job_extra_size,ch.job_difficulty,self.mask,getattr(ch,'clean_generation',0))
            extra,size,_ = self.capabilities.layout(snapshot[1],snapshot[2])
            context = (ch,extra,size)
            if self.client_context is not None and self.client_context != context:
                if not self.can_hot_switch():
                    self.schedule_reconnect()
                    return
                self.write_client({'id':None,'method':'mining.set_extranonce','params':[extra,size]})
                self.last_transition = (time.monotonic(),'extranonce')
                clean = True
            self.client_context = context
            packet = self.make_job(ch, clean,snapshot)
            if getattr(self.relay, "trace_stratum", False):
                stratum_trace("proxy_to_asic", self.session["id"], ch.target, packet,
                              (self.user_password, self.fee_password, *self.auth.values()))
            self.note_difficulty(snapshot[3])
            self.write_client({"id": None, "method": "mining.set_difficulty", "params": [snapshot[3]]})
            self.write_client(packet)
            await self.drain_client()
        if self.current is ch and self.logged_channel is not ch:
            self.relay.event(self.session, ch.target, kind="assignment")
            self.logged_channel = ch

    async def activate(self, ch):
        self.check_fee_policy()
        clean_revision = self.clean_revision
        if not self.can_hot_switch():
            await self.request_reconnect()
            return False
        async with self.switch_lock:
            if ch.closed or not ch.ready.is_set() or ch.job is None or ch.job_difficulty is None:
                return False
            async with self.write_lock:
                if (ch.closed or not ch.ready.is_set() or ch.job is None or ch.job_difficulty is None
                        or ch.job_extranonce != ch.extranonce1
                        or ch.job_extra_size != ch.extranonce2_size):
                    return False
                if ch.target == 'fee' and (self.clean_revision != clean_revision
                        or self.urgent_job is not None or self.urgent_inflight is not None):
                    return False
                extra, size, _ = self.capabilities.layout(ch.job_extranonce, ch.job_extra_size)
                self.current = ch
                self.client_context = (ch,extra,size)
                self.session["pool"] = "primary" if ch.target == "user" else ch.target
                self.session["observer"].protocol.update(extranonce1=extra,
                    extranonce2_size=size, observed_difficulty=float(ch.job_difficulty),
                    upstream_extranonce1=ch.job_extranonce, upstream_extranonce2_size=ch.job_extra_size)
                self.last_transition = (time.monotonic(), 'extranonce')
                for packet in (
                    {"id": None, "method": "mining.set_extranonce", "params": [extra, size]},
                    {"id": None, "method": "mining.set_difficulty", "params": [ch.job_difficulty]},
                    self.make_job(ch, True),
                ):
                    if getattr(self.relay, "trace_stratum", False):
                        stratum_trace("proxy_to_asic", self.session["id"], ch.target, packet,
                                      (self.user_password, self.fee_password, *self.auth.values()))
                    self.write_client(packet)
                await self.drain_client()
                self.last_transition = (time.monotonic(), 'extranonce')
                self.last_client_difficulty = ch.job_difficulty
            self.relay.event(self.session, ch.target)
            self.logged_channel = ch
            return True

    async def disconnected(self, ch):
        if self.stopping:
            return
        event(logging.WARNING, "upstream_disconnected", session_id=self.session["id"], target=ch.target)
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
            if target is None or target.closed or not self.can_hot_switch() or not await self.activate(target):
                self.close_client()
        except (OSError, ValueError, ProtocolError, asyncio.TimeoutError):
            self.close_client()
        finally:
            if ch.target == 'fee' or self.current is not None and self.current.target != 'fee':
                self.allocator.release(self.session['id'])

    async def reconcile(self):
        async with self.reconcile_lock:
            if (self.stopping or self.route_updating or not self.fee or self.current is None
                    or self.session.get('pending',0)):
                return
            fee = self.channels.get('fee')
            user = next((item for name in ('user','backup')
                         if (item := self.channels.get(name)) and not item.closed),None)
            available = bool(fee and not fee.closed and fee.authorized and fee.ready.is_set())
            can_enter = self.current.target == 'fee' or self.jitter.remaining() == 0
            want = (available and can_enter and self.allocator.acquire(self.session['id'],fee.job_difficulty))
            if self.current.target == 'fee' and not want:
                self.allocator.release(self.session['id'])
                if self.can_hot_switch() and user and not user.closed:
                    if not await self.activate(user):
                        self.close_client()
                    self.jitter.defer()
                elif not self.can_hot_switch():
                    await self.request_reconnect()
                else:
                    # Fail closed rather than mine indefinitely to the fee account.
                    self.close_client()
            elif self.current.target != 'fee' and want:
                if self.can_hot_switch():
                    if not await self.activate(fee) and self.current is not fee:
                        self.allocator.release(self.session['id'])
                else:
                    # A fresh TCP session must reserve again using its own ID.
                    self.allocator.release(self.session['id'])
                    if time.monotonic() - self.started >= self.relay.reconnect_min_seconds:
                        await self.request_reconnect()

    async def reply(self, ident, response):
        await self.send({"id": ident, "result": response.get("result"), "error": response.get("error")})

    async def submit(self, ident, params):
        received_at = time.monotonic()
        self.check_fee_policy()
        metrics = self.session["metrics"]
        metrics.submitted += 1
        category = "malformed_local"
        try:
            if not isinstance(params, list) or len(params) not in (5, 6) or not all(isinstance(x, str) for x in params):
                raise ValueError("malformed share")
            if getattr(self.relay, "trace_stratum", False):
                stratum_trace("asic_to_proxy", self.session["id"], "router",
                              {"id": ident, "method": "mining.submit", "params": params},
                              (self.user_password, self.fee_password, *self.auth.values()))
            timing = self.job_times.get(params[1])
            if timing is not None and received_at - timing[0] >= getattr(self.relay, 'job_ttl', 900):
                self.jobs.pop(params[1], None)
                self.job_times.pop(params[1], None)
                self.job_layouts.pop(params[1], None)
            if params[0] not in self.auth or params[1] not in self.jobs:
                category = "unknown_job_local"
                raise ValueError("unknown worker or job")
            ch, upstream_id, job, extra, size, difficulty, mask = self.jobs[params[1]]
            client_extra, client_size, prefix = self.job_layouts.get(params[1], (extra, size, ''))
            if len(params[2]) != client_size * 2 or any(c not in string.hexdigits for c in params[2]):
                raise ValueError('invalid extranonce2 width')
            if ch.closed or ch.extranonce1 != extra or ch.extranonce2_size != size:
                category = "unknown_job_local"
                raise ValueError("job session is no longer valid")
            key = (id(ch), upstream_id, extra, prefix + params[2], *params[3:])
            if key in self.duplicates:
                category = "duplicate_local"
                raise ValueError("duplicate")
            if self.relay.validate_pow:
                valid = validate_stratum_share(job, client_extra, params[2], params[3], params[4],
                    params[5] if len(params) == 6 else None, mask, difficulty, extranonce2_size=client_size)
                if not valid:
                    # Never suppress a potential network block when a pool has
                    # assigned difficulty above the network target.
                    header = build_stratum_header(job, client_extra, params[2], params[3], params[4],
                        params[5] if len(params) == 6 else None, mask)
                    if not meets_target(header, compact_target(int(job[6], 16))):
                        category = "low_diff_local"
                        raise ValueError("low difficulty")
            self.duplicates[key] = (params[1], time.monotonic())
            while len(self.duplicates) > self.MAX_DUPLICATES:
                self.duplicates.popitem(last=False)
            self.last_miner_activity = time.monotonic()
        except (ValueError, TypeError):
            metrics.invalid_local += 1
            event(logging.WARNING, "share_rejected_local", session_id=self.session["id"], reason=category)
            setattr(metrics, category, getattr(metrics, category) + 1)
            if params and isinstance(params[0], str) and params[0] in self.auth:
                self.relay.workers.record(self.session["id"], params[0], "invalid_local")
            await self.reply(ident, {"result": False, "error": [23, "Invalid or unknown share", None]})
            return
        outbound = list(params)
        outbound[1] = upstream_id
        outbound[2] = prefix + params[2]
        if ch.target == "fee":
            outbound[0] = self.fee_worker
        elif self.user_worker:
            outbound[0] = self.user_worker
        fee = self.channels.get("fee")
        fee_available = bool(fee and not fee.closed and fee.authorized and fee.ready.is_set())
        if getattr(self.relay, "trace_stratum", False):
            stratum_trace("proxy_to_upstream", self.session["id"], ch.target,
                          {"id": ident, "method": "mining.submit", "params": outbound},
                          (self.user_password, self.fee_password, *self.auth.values()))
        responded = False
        fee_token = self.allocator.begin_submit(self.session['id'],difficulty) if ch.target == 'fee' else None
        pending = True
        try:
            self.session["pending"] = self.session.get("pending", 0) + 1
            sent_at = time.monotonic()
            response = await ch.request("mining.submit", outbound)
            responded = True
            latency = max(0, (time.monotonic() - sent_at) * 1000)
            metrics.share_rtt_samples += 1
            metrics.share_rtt_total_ms += max(0, (time.monotonic() - sent_at) * 1000)
            if ch.target == "fee":
                if response.get("result") is True and response.get("error") is None:
                    self.health.share_result(True)
                elif response.get("result") is False or response.get("error") is not None:
                    self.health.share_result(False)
            if response.get("result") is True and response.get("error") is None:
                self.last_transition = None
                metrics.accepted += 1
                timing = self.job_times.get(params[1])
                if timing is not None and params[0] not in timing[1]:
                    timing[1].add(params[0])
                    self.relay.workers.first_share(self.session["id"], params[0],
                                                  max(0, received_at - timing[0]) * 1000)
                self.relay.ledger.record("fee" if ch.target == "fee" else "user", difficulty,
                                         fee_available=fee_available)
                self.relay.metrics._audit.record("fee" if ch.target == "fee" else "user", difficulty)
            elif response.get("result") is False or response.get("error") is not None:
                metrics.rejected += 1
                error = response.get("error")
                code = error[0] if isinstance(error, list) and error and type(error[0]) is int else None
                event(logging.WARNING, "share_rejected_pool", session_id=self.session["id"], target=ch.target, code=code)
            else:
                metrics.unmatched += 1
            error = response.get("error")
            outcome = ("accepted" if response.get("result") is True and error is None else
                       "rejected" if response.get("result") is False or error is not None else "unknown")
            self.relay.workers.record(self.session["id"], params[0], outcome, difficulty=difficulty,
                                      rtt_ms=latency, stale=isinstance(error, list) and bool(error) and error[0] == 21)
            if fee_token is not None:
                self.allocator.finish_submit(fee_token,outcome)
                fee_token = None
            await self.reply(ident, response)
            self.session['pending'] -= 1
            pending = False
            await self.reconcile()
        except (ConnectionError, asyncio.TimeoutError):
            # A failed write to an ASIC after a pool confirmation does not
            # change that share's outcome or make the pool unavailable.
            if responded:
                raise
            if ch.target == "fee":
                await ch.close()
            metrics.unmatched += 1
            self.relay.workers.record(self.session["id"], params[0], "unknown")
            await self.reply(ident, {"result": None, "error": [20, "Upstream unavailable", None]})
        except asyncio.CancelledError:
            if not responded:
                metrics.unmatched += 1
                self.relay.workers.record(self.session["id"], params[0], "unknown")
            raise
        finally:
            if fee_token is not None:
                self.allocator.finish_submit(fee_token,'unknown')
            if pending:
                self.session["pending"] -= 1

    async def run(self):
        self.owner = asyncio.current_task()
        self.tasks.add(asyncio.create_task(self.watch_miner()))
        self.tasks.add(asyncio.create_task(self.collect_jobs()))
        self.tasks.add(asyncio.create_task(self.dispatch_notifications()))
        try:
            self.check_fee_policy()
            first = await self.packets.read_packet()
            if first is None:
                return
            self.session["protocol_started"] = True
            event(logging.INFO, "session_started", session_id=self.session["id"], active_router=True)
            self.prefer_fee = bool(self.fee and self.relay.ledger.should_fee())
            try:
                user = await self.connect("user", self.session["endpoint"])
            except (OSError, asyncio.TimeoutError):
                self.relay.metrics.upstream_failures += 1
                if not self.relay.backup:
                    raise
                user = await self.connect("backup", self.relay.backup)
                self.relay.metrics.backup_connections += 1
            self.current = user
            self.session["state"], self.session["pool"] = "connected", "primary" if user.target == "user" else user.target
            async for packet in self.miner_packets(first):
                ident, method, params = packet.get("id"), packet.get("method"), packet.get("params", [])
                if (not isinstance(ident, (int, str)) or isinstance(ident, bool) or
                    not isinstance(method, str) or not isinstance(params, list)):
                    raise ProtocolError("invalid miner request")
                if (isinstance(ident, str) and len(ident) > 128) or (isinstance(ident, int) and ident.bit_length() > 256):
                    raise ProtocolError("request identifier too large")
                if method == "mining.configure":
                    validate_config(params)
                    self.config = merge_config(self.config, params, getattr(self.relay,'max_miner_message',4096))
                    response = await user.request(method, params)
                    result = response.get("result")
                    old_mask = self.mask
                    if 'version-rolling' in params[0]:
                        self.mask = negotiated_mask(params, response)
                    if self.auth and old_mask != self.mask:
                        await self.reply(ident, response)
                        await self.request_reconnect()
                        continue
                    if 'subscribe-extranonce' in params[0] and response.get('error') is None:
                        response = dict(response, result=dict(result or {}, **{'subscribe-extranonce': True}))
                        result = response['result']
                        self.hot = True
                        self.session['observer'].protocol['extranonce_subscribed'] = True
                    fee = self.channels.get("fee")
                    if fee is not None:
                        try:
                            fee_params = upstream_config(self.config, self.mask)
                            fee_response = await fee.request(method, fee_params)
                            compatible = self.mask is None or negotiated_mask(fee_params, fee_response) == self.mask
                        except (KeyError, TypeError, ValueError, OSError, asyncio.TimeoutError):
                            compatible = False
                        if not compatible:
                            if self.current is fee:
                                raise ProtocolError("incompatible active mask")
                            self.channels.pop("fee", None)
                            await fee.close()
                    if isinstance(result, dict) and response.get("error") is None:
                        self.session["observer"].protocol.update(configured=True,
                            version_rolling=self.mask is not None, version_mask=self.mask)
                        if self.mask is not None:
                            self.session['observer'].protocol['version_rolling_degraded'] = (
                                mask_value(self.mask).bit_count() < self.config[1].get('version-rolling.min-bit-count',0))
                    await self.reply(ident, response)
                elif method == "mining.subscribe":
                    self.subscription = params
                    self.capabilities.agent = params[0][:128] if params and isinstance(params[0],str) else ''
                    response = await user.request(method, params)
                    self.subscription_result(user, response.get("result"))
                    if self.fee and (not self.jitter.maximum or self.prefer_fee):
                        try:
                            fee = await self.prepare("fee", self.fee, fee=True)
                            if self.prefer_fee:
                                await asyncio.wait_for(fee.ready.wait(),self.relay.timeout)
                                if (not fee.closed and fee.authorized and fee.job_difficulty is not None
                                        and self.allocator.acquire(self.session['id'],fee.job_difficulty)):
                                    self.current = fee
                                    response = dict(response,result=fee.subscription_result)
                        except (OSError, ValueError, ProtocolError, asyncio.TimeoutError):
                            fee = self.channels.pop("fee", None)
                            if fee:
                                await fee.close()
                            self.allocator.release(self.session['id'])
                    response = dict(response, result=self.capabilities.subscription(response['result']))
                    self.client_context = (self.current,response['result'][1],response['result'][2])
                    self.session["observer"].protocol.update(subscribed=True,
                        extranonce1=response['result'][1], extranonce2_size=response['result'][2],
                        upstream_extranonce1=self.current.extranonce1,
                        upstream_extranonce2_size=self.current.extranonce2_size)
                    await self.reply(ident, response)
                elif method == "mining.extranonce.subscribe":
                    self.hot = True
                    self.session["observer"].protocol["extranonce_subscribed"] = True
                    await self.reply(ident, {"result": True, "error": None})
                elif method == "mining.authorize":
                    if len(params) >= 2 and isinstance(params[1], str):
                        remember_secret(params[1])
                    upstream_params = [self.user_worker, self.user_password] if self.user_worker else params
                    response = await user.request(method, upstream_params)
                    accepted = response.get("result") is True and response.get("error") is None
                    if accepted and len(params) >= 2 and all(isinstance(x, str) for x in params[:2]):
                        if len(self.auth) >= 32 and params[0] not in self.auth:
                            raise ProtocolError("too many workers")
                        if len(params[0]) > 128 or len(params[1]) > 1024:
                            raise ProtocolError("credentials too long")
                        self.auth[params[0]] = params[1]
                        self.last_miner_activity = time.monotonic()
                        if not self.jitter.maximum or self.channels.get('fee') is not None:
                            self.start_fee_policy()
                        self.relay.workers.authorize(self.session["id"], params[0], self.session["ip"])
                        user.authorized = True
                        self.session["worker"] = "".join(c if c.isprintable() else "?" for c in params[0][:128])
                        self.session["observer"].protocol["authorized_workers"] = list(self.auth)
                        self.session['observer'].protocol['switch_policy'] = 'hot' if self.can_hot_switch() else 'reconnect'
                        fee = self.channels.get("fee")
                        if fee and not fee.authorized:
                            try:
                                fee_response = await fee.request(method, [self.fee_worker, self.fee_password])
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
                    self.schedule_fee_warmup()
                    self.prefer_fee = False
                elif method == "mining.submit":
                    await self.submit(ident, params)
                elif method == "mining.ping":
                    if params:
                        raise ProtocolError("invalid ping")
                    if self.auth:
                        self.last_miner_activity = time.monotonic()
                    await self.reply(ident, {"result": True, "error": None})
                else:
                    await self.reply(ident, await self.current.request(method, params))
        except ProtocolError:
            self.intentional_close = True
            self.relay.metrics.protocol_drops += 1
            self.abort(self.writer)
        except (OSError, ValueError, TypeError, asyncio.TimeoutError):
            self.relay.metrics.transport_errors += 1
        finally:
            if (self.client_disconnected and not self.intentional_close and self.last_transition and
                    self.auth and time.monotonic() - self.last_transition[0] <= 3):
                try:
                    await self.relay.compatibility.remember(self.session.get('ip',''), self.auth,
                        self.capabilities.agent, self.last_transition[1])
                    self.relay.metrics.compatibility_fallbacks += 1
                    event(logging.WARNING, "reconnect_fallback", session_id=self.session["id"])
                except (OSError, ValueError):
                    event(logging.WARNING, 'compatibility_checkpoint_failed', session_id=self.session['id'])
            self.stopping = True
            self.allocator.release(self.session['id'])
            for task in list(self.tasks):
                task.cancel()
            if self.tasks:
                await asyncio.gather(*self.tasks, return_exceptions=True)
            for ch in list(self.channels.values()) + self.retired:
                await ch.close()
            if self.fee_slot is not None:
                self.relay.fee_slots.release(self.session['id'])
            self.packets.pending.clear()
            self.packets.decoder.buffer.clear()
            self.jobs.clear()
            self.job_times.clear()
            self.job_layouts.clear()
            self.duplicates.clear()
            self.urgent_job = self.normal_job = None
            self.controls.clear()
