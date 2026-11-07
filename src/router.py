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
from decimal import Decimal

from upstream_manager import UpstreamConnection, ProtocolError, encode_packet, open_upstream, UpstreamLoopError, parse_endpoint
from pow_validator import (validate_stratum_share, target_from_difficulty,
                           build_stratum_header, compact_target, meets_target)
from devfee_scheduler import (FeeHealth, FeeJitter, independent_subscription, FairnessAllocator,
                              stable_fee_worker)
from network_io import write_bounded, drain_bounded
from pilot_logging import event, stratum_trace, remember_secret
from stratum_codec import PacketReader
from hardware_compatibility import (ClientCapabilities, validate_config, negotiated_mask,
                                    upstream_config, mask_value, merge_config)
from aggregation import (AggregationJob, AggregationKey, NonceLease, NoncePartitioner,
                         ShareTargets, virtual_subscription_job)


class AggregatedJobRouter:
    """Bounded per-miner job and proof state for a shared pool connection.

    A locally accepted share is not necessarily forwarded or pool-confirmed.
    The integration must record pool outcomes only after the pool replies.
    """
    def __init__(self, lease, mask=None, *, max_jobs=128, max_shares=8192, ttl=900):
        if type(max_jobs) is not int or max_jobs < 1 or type(max_shares) is not int or max_shares < 1 or ttl <= 0:
            raise ValueError('invalid aggregation limits')
        self.lease, self.mask = lease, mask
        self.max_jobs, self.max_shares, self.ttl = max_jobs, max_shares, ttl
        self.jobs, self.shares = OrderedDict(), OrderedDict()
        self.sequence = 0

    def prune(self, now):
        expired = [key for key, (_, created, _) in self.jobs.items() if now - created >= self.ttl]
        for key in expired:
            self.jobs.pop(key, None)
        for key, created in list(self.shares.items()):
            if now - created >= self.ttl:
                self.shares.pop(key, None)

    def issue(self, params, local_difficulty, pool_difficulty, now=None):
        now = time.monotonic() if now is None else now
        self.prune(now)
        targets = ShareTargets.from_difficulties(local_difficulty, pool_difficulty, params[6])
        job = AggregationJob(params, self.lease, targets, self.mask)
        self.sequence += 1
        local_id = format(self.sequence, 'x')
        self.jobs[local_id] = (job, now, pool_difficulty)
        while len(self.jobs) > self.max_jobs:
            self.jobs.popitem(last=False)
        message = list(params)
        message[1:] = [list(v) if isinstance(v, tuple) else v for v in job.params[1:]]
        message[0] = local_id
        return message

    def submit(self, params, now=None):
        now = time.monotonic() if now is None else now
        self.prune(now)
        if not isinstance(params, list) or len(params) not in (5, 6) or not all(isinstance(v, str) for v in params):
            raise ValueError('malformed share')
        entry = self.jobs.get(params[1])
        if entry is None:
            raise ValueError('unknown job')
        job = entry[0]
        # The pool identifies duplicates by upstream job and proof, not our
        # local job alias. Ignore worker name when forming the duplicate key.
        proof = (job.params[0], *[v.lower() for v in params[2:]])
        if proof in self.shares:
            raise ValueError('duplicate share')
        if len(self.shares) >= self.max_shares:
            raise ValueError('share registry full')
        decision = job.classify_submit(params[2], params[3], params[4], params[5] if len(params) == 6 else None)
        if not decision.accept_locally:
            raise ValueError('low difficulty')
        self.shares[proof] = now
        upstream = list(params)
        upstream[1] = job.params[0]
        upstream[2] = job.lease.upstream_extranonce2(params[2])
        return decision, upstream if decision.forward else None


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
        # A job belongs to the credentials of the connection that issued it.
        # Retain those credentials if a later route changes the worker name.
        ch.submit_worker = self.fee_worker if name == 'fee' else self.user_worker
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
        assigned_worker = getattr(ch, 'submit_worker',
                                  self.fee_worker if ch.target == 'fee' else self.user_worker)
        if assigned_worker:
            outbound[0] = assigned_worker
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
class AggregateSession:
    """Shared user and optional fee routes with a stable virtual extranonce."""

    def __init__(self, relay, reader, writer, session):
        self.relay, self.writer, self.session = relay, writer, session
        self.reader = PacketReader(reader, relay.max_miner_message, role='miner', metrics=relay.metrics)
        self.identity = str(session['id'])
        self.endpoint = session['endpoint']
        self.route_worker = getattr(relay, 'user_worker', '')
        self.route_password = getattr(relay, 'user_password', 'x')
        self.agent = None
        self.worker = None
        self.group = None
        self.jobs = None
        self.prefix = None
        self.width = None
        self.write_lock = asyncio.Lock()
        self.job_generation = None
        self.fee_group = None
        self.fee_jobs = None
        self.fee_generation = None
        self.fee_task = None
        self.fee_disabled = False
        self.route = 'user'
        self.route_lock = asyncio.Lock()
        self.pending_fee = False
        self.fee_endpoint = relay.fee
        self.fee_worker = relay.fee_worker
        self.fee_password = relay.fee_password
        self.version_mask = None
        self.version_probed = False

    async def send(self, *packets):
        async with self.write_lock:
            for packet in packets:
                write_bounded(self.writer, encode_packet(packet), self.relay.metrics)
            await drain_bounded(self.writer, self.relay.timeout, metrics=self.relay.metrics)

    async def reply(self, ident, result, error=None):
        await self.send({'id': ident, 'result': result, 'error': error})

    async def configure(self, ident, params):
        validate_config(params)
        extensions, options = params
        result = dict.fromkeys(extensions, False)
        if 'version-rolling' in extensions:
            requested = options.get('version-rolling.mask', 'ffffffff')
            minimum_bits = options.get('version-rolling.min-bit-count', 0)
            if self.version_mask is not None and mask_value(self.version_mask) & ~mask_value(requested):
                await self.reply(ident, None, [20, 'Version mask cannot change during this session', None])
                return
            if self.worker is None and not self.version_probed:
                self.version_probed = True
                self.version_mask = await probe_aggregate_mask(self.relay, self.endpoint, requested, minimum_bits)
            if self.version_mask is not None:
                result.update({'version-rolling': True, 'version-rolling.mask': self.version_mask})
                self.session['observer'].protocol.update(
                    configured=True, version_rolling=True, version_mask=self.version_mask,
                    version_rolling_degraded=mask_value(self.version_mask).bit_count() < minimum_bits)
        await self.reply(ident, result)

    async def notify(self, packet, route='user'):
        async with self.route_lock:
            if self.route == route:
                await self.publish(packet, route)

    async def publish(self, packet, route, *, clean=False):
        if packet.get('method') != 'mining.notify':
            return
        group, jobs = (self.fee_group, self.fee_jobs) if route == 'fee' else (self.group, self.jobs)
        channel = group.connection
        generation = packet['generation']
        if channel.closed or generation != channel.generation:
            return
        attr = 'fee_generation' if route == 'fee' else 'job_generation'
        if getattr(self, attr) != generation:
            jobs.jobs.clear()
            setattr(self, attr, generation)
        difficulty = packet['difficulty']
        local = Decimal(str(difficulty)) / 2
        translated = virtual_subscription_job(packet['params'], channel.extranonce1,
                                              channel.extranonce2_size, self.prefix, self.width)
        params = jobs.issue(translated, local, difficulty)
        params[0] = route[0] + params[0]
        if clean:
            params[8] = True
        await self.send({'id': None, 'method': 'mining.set_difficulty', 'params': [local]},
                        {'id': None, 'method': 'mining.notify', 'params': params})

    async def close_client(self):
        self.writer.close()

    async def fee_unavailable(self):
        async with self.route_lock:
            self.fee_disabled = True
            self.relay.fee_allocator.release(self.session['id'])
            if self.route == 'fee':
                self.route = 'user'
                self.session['pool'] = 'primary'
                if not self.group.connection.closed and self.group.connection.job:
                    await self.publish(self.group.connection.job, 'user', clean=True)

    async def run_fee(self):
        group = None
        try:
            jitter = FeeJitter(self.relay.fee_jitter_seconds)
            await self.relay.fee_connection_gate.wait(jitter.remaining())
            host, port, tls = parse_endpoint(*self.fee_endpoint)
            key = AggregationKey(host, port, tls or self.relay.tls, self.fee_worker,
                                 self.fee_password, version_mask=self.version_mask, user_agent=self.agent)
            if key == self.group.key:
                return
            group, _ = await self.relay.shared_upstreams.acquire(key, self.identity)
            channel = group.connection
            if channel.extranonce2_size < len(self.prefix) // 2 + self.width:
                return
            self.fee_group = group
            self.fee_jobs = AggregatedJobRouter(self.jobs.lease, self.version_mask, ttl=self.relay.job_ttl)
            group.subscribe(self.identity, self.notify_fee, self.fee_unavailable)
            while not self.fee_disabled and not channel.closed and self.identity in group.subscribers:
                await self.reconcile_fee()
                await asyncio.sleep(.25)
        except (OSError, ValueError, ProtocolError, asyncio.TimeoutError):
            self.relay.metrics.upstream_failures += 1
        finally:
            try:
                if not self.writer.is_closing():
                    await self.fee_unavailable()
            finally:
                if group is not None:
                    await self.relay.shared_upstreams.release(group, self.identity)

    async def notify_fee(self, packet):
        await self.notify(packet, 'fee')

    def fee_ready(self):
        return (not self.fee_disabled and self.fee_group is not None and not self.fee_group.connection.closed
                and self.identity in self.fee_group.subscribers
                and self.fee_group.connection.job is not None)

    async def reconcile_fee(self):
        async with self.route_lock:
            if self.pending_fee or not self.fee_ready():
                return
            fee_job = self.fee_group.connection.job
            want = self.relay.fee_allocator.acquire(self.session['id'], fee_job['difficulty'])
            route = 'fee' if want else 'user'
            if route != self.route:
                self.route = route
                self.session['pool'] = 'fee' if want else 'primary'
                packet = fee_job if want else self.group.connection.job
                if packet is not None:
                    await self.publish(packet, route, clean=True)
                self.relay.event(self.session, route)

    async def authorize(self, ident, params):
        if self.agent is None or self.worker is not None or len(params) != 2 or not all(
                isinstance(value, str) for value in params):
            raise ProtocolError('invalid aggregated authorization')
        worker, password = params
        if not worker or len(worker) > 128 or len(password) > 1024:
            raise ProtocolError('invalid aggregated credentials')
        remember_secret(password)
        host, port, tls = parse_endpoint(*self.endpoint)
        key = AggregationKey(host, port, tls or self.relay.tls,
                             self.route_worker or worker,
                             self.route_password if self.route_worker else password,
                             version_mask=self.version_mask, user_agent=self.agent)
        self.group, _ = await self.relay.shared_upstreams.acquire(key, self.identity)
        channel = self.group.connection
        available = channel.extranonce2_size - len(self.prefix) // 2
        if available < self.relay.asic_extranonce2_min:
            await self.reply(ident, False, [20, 'Pool nonce space cannot support aggregation', None])
            return False
        if available < self.width:
            self.relay.aggregate_sizes.remember(self.hint, channel.extranonce2_size)
            await self.send({'id': None, 'method': 'client.reconnect', 'params': []})
            return False
        # The translated job presents the virtual prefix as extranonce1.
        lease = NonceLease(self.identity, self.prefix, self.width, '', self.width)
        self.jobs = AggregatedJobRouter(lease, self.version_mask, ttl=self.relay.job_ttl)
        self.worker = worker
        self.session.update(state='connected', pool='primary', worker=worker, protocol_started=True)
        self.session['observer'].protocol.update(subscribed=True, extranonce1=self.prefix,
                                                 extranonce2_size=self.width, authorized_workers=[worker])
        self.relay.workers.authorize(self.session['id'], worker, self.session['ip'])
        # Capture the latest job and register synchronously so no notification
        # can slip between taking the snapshot and joining the broadcast.
        await self.reply(ident, True)
        if channel.closed:
            return False
        initial = channel.job
        self.group.subscribe(self.identity, self.notify, self.close_client)
        if initial is not None:
            await self.notify(initial)
        if self.fee_endpoint:
            self.fee_task = asyncio.create_task(self.run_fee())
        return True

    async def submit(self, ident, params):
        metrics = self.session['metrics']
        metrics.submitted += 1
        try:
            if len(params) < 2 or params[0] != self.worker or not isinstance(params[1], str) or not params[1]:
                raise ValueError('unauthorized share')
            route = {'u': 'user', 'f': 'fee'}.get(params[1][0])
            if route is None:
                raise ValueError('unknown route')
            group, jobs, generation = ((self.fee_group, self.fee_jobs, self.fee_generation) if route == 'fee'
                                       else (self.group, self.jobs, self.job_generation))
            if group is None or jobs is None:
                raise ValueError('unknown job')
            channel = group.connection
            if channel.closed or channel.generation != generation:
                raise ValueError('stale job')
            params = list(params)
            params[1] = params[1][1:]
            _, outbound = jobs.submit(params)
        except ValueError:
            metrics.invalid_local += 1
            await self.reply(ident, False, [23, 'Invalid, stale or duplicate share', None])
            return
        if outbound is None:
            await self.reply(ident, True)
            return
        difficulty = jobs.jobs[params[1]][2]
        outbound[0] = group.key.worker
        padding = channel.extranonce2_size - len(self.prefix) // 2 - self.width
        outbound[2] = self.prefix + outbound[2] + '00' * padding
        allocator = self.relay.fee_allocator
        token = allocator.begin_submit(self.session['id'], difficulty) if route == 'fee' else None
        self.pending_fee = token is not None
        outcome = 'unknown'
        try:
            response = await channel.request('mining.submit', outbound)
            if response.get('result') is True and response.get('error') is None:
                outcome = 'accepted'
            elif response.get('result') is False or response.get('error') is not None:
                outcome = 'rejected'
        except (OSError, asyncio.TimeoutError):
            response = {'result': None, 'error': [20, 'Upstream unavailable', None]}
        finally:
            if token is not None:
                allocator.finish_submit(token, outcome)
            self.pending_fee = False
        if outcome == 'accepted':
            metrics.accepted += 1
            self.relay.ledger.record(route, difficulty, fee_available=self.fee_ready())
            self.relay.metrics._audit.record(route, difficulty)
        elif outcome == 'rejected':
            metrics.rejected += 1
        else:
            metrics.unmatched += 1
        self.relay.workers.record(self.session['id'], self.worker, outcome, difficulty=difficulty)
        await self.reply(ident, response.get('result'), response.get('error'))
        if route == 'fee' and outcome != 'accepted':
            await self.fee_unavailable()
        await self.reconcile_fee()

    async def run(self):
        handshake_deadline = time.monotonic() + self.relay.handshake_timeout
        try:
            while True:
                timeout = (self.relay.miner_timeout or None) if self.worker else max(
                    0, handshake_deadline - time.monotonic())
                packet = await asyncio.wait_for(self.reader.read_packet(), timeout)
                if packet is None:
                    return
                ident, method, params = packet.get('id'), packet.get('method'), packet.get('params', [])
                if type(ident) not in (str, int) or not isinstance(params, list):
                    raise ProtocolError('invalid miner request')
                if method == 'mining.configure':
                    await self.configure(ident, params)
                elif method == 'mining.subscribe' and self.agent is None:
                    self.agent = params[0] if params else ''
                    if not isinstance(self.agent, str) or len(self.agent) > 128:
                        raise ProtocolError('invalid user agent')
                    # Subscribe has no credentials. The hint affects only width,
                    # while the shared socket is selected by exact credentials.
                    self.hint = (self.endpoint, self.session['ip'], self.agent, self.route_worker)
                    hint = self.relay.aggregate_sizes.get(self.hint)
                    allocator = self.relay.aggregate_prefixes
                    self.width = allocator.local_extranonce2_size
                    if hint is not None:
                        self.width = min(self.width, max(self.relay.asic_extranonce2_min,
                                                         hint - allocator.prefix_bytes))
                    self.prefix = allocator.allocate(self.identity)
                    await self.reply(ident, [[['mining.notify', self.identity]], self.prefix, self.width])
                elif method == 'mining.authorize':
                    if not await self.authorize(ident, params):
                        return
                elif method in ('mining.extranonce.subscribe', 'mining.ping') and not params:
                    await self.reply(ident, True)
                elif method == 'mining.submit':
                    await self.submit(ident, params)
                else:
                    await self.reply(ident, False, [20, 'Unsupported request', None])
        except (OSError, ValueError, ProtocolError, asyncio.TimeoutError):
            self.relay.metrics.transport_errors += 1
        finally:
            if self.fee_task is not None:
                self.fee_task.cancel()
                await asyncio.gather(self.fee_task, return_exceptions=True)
            self.relay.fee_allocator.release(self.session['id'])
            if self.group is not None:
                await self.relay.shared_upstreams.release(self.group, self.identity)
            self.relay.aggregate_prefixes.release(self.identity)
            if not self.relay.shared_upstreams.groups:
                self.relay.aggregate_prefixes.reset_if_idle()


async def probe_aggregate_mask(relay, endpoint, requested, minimum_bits):
    """Negotiate before the miner supplies account credentials or starts work."""
    async def ignore(*args):
        pass

    channel = None
    try:
        reader, writer = await open_upstream(endpoint, relay.tls_context, relay.timeout,
                                             relay.url_tls_context, relay.resolver,
                                             address_guard=relay.is_self_route)
        channel = UpstreamConnection(reader, writer, 'user', ignore, ignore,
                                     relay.timeout, metrics=relay.metrics)
        return await channel.configure_version_rolling(requested, minimum_bits)
    except (OSError, ValueError, ProtocolError, asyncio.TimeoutError):
        return None
    finally:
        if channel is not None:
            await channel.close()


async def open_aggregate_channel(relay, key):
    """Pool-side state and its reader task live independently of any ASIC."""
    async def notification(channel, packet):
        method, params = packet.get('method'), packet.get('params')
        if method == 'mining.set_difficulty':
            if not isinstance(params, list) or len(params) != 1:
                raise ProtocolError('invalid pool difficulty')
            target_from_difficulty(params[0])
            channel.difficulty = params[0]
        elif method == 'mining.notify':
            if not isinstance(params, list) or len(params) != 9 or type(params[8]) is not bool or channel.difficulty is None:
                raise ProtocolError('invalid aggregate job')
            if params[8]:
                channel.generation += 1
            packet.update(generation=channel.generation, difficulty=channel.difficulty)
            channel.job = packet
            channel.ready.set()
        elif method == 'mining.set_version_mask':
            if (not isinstance(params, list) or len(params) != 1 or key.version_mask is None
                    or mask_value(params[0]) != mask_value(key.version_mask)):
                raise ProtocolError('pool changed its shared version mask')
        elif method in ('mining.set_extranonce', 'client.reconnect'):
            raise ProtocolError('pool changed its shared connection context')

    async def disconnected(channel):
        pass

    host = key.host
    if key.use_tls:
        host = 'stratum+ssl://' + (f'[{host}]' if ':' in host else host)
    reader, writer = await open_upstream((host, key.port), relay.tls_context, relay.timeout,
                                         relay.url_tls_context, relay.resolver,
                                         address_guard=relay.is_self_route)
    channel = UpstreamConnection(reader, writer, 'user', notification, disconnected,
                                 relay.timeout, metrics=relay.metrics)
    channel.generation = 0
    try:
        if key.version_mask is not None:
            if await channel.configure_version_rolling(key.version_mask) != key.version_mask:
                raise ProtocolError('pool did not preserve the negotiated shared version mask')
        response = await channel.request('mining.subscribe', [key.user_agent])
        StatefulRouter.subscription_result(channel, response.get('result'))
        response = await channel.request('mining.authorize', [key.worker, key.password])
        if response.get('result') is not True or response.get('error') is not None:
            raise ProtocolError('pool authorization rejected')
        channel.authorized = True
        partitioner = NoncePartitioner(channel.extranonce1, channel.extranonce2_size, 0,
                                       maximum_workers=50)
        return channel, partitioner
    except BaseException:
        await channel.close()
        raise
