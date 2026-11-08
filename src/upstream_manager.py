"""Upstream transports: legacy selection and bounded stateful RPC channels."""
import asyncio
import json
import ssl
import logging
import re
import socket
import copy
import time
from pilot_logging import event
from decimal import Decimal
from stratum_codec import ProtocolError, PacketReader
from urllib.parse import urlsplit
from functools import lru_cache
from network_io import AsyncResolver, configure_writer, write_bounded, drain_bounded
from aggregation import AggregationKey, InsufficientNonceSpace, NoncePartitioner
from hardware_compatibility import negotiated_mask

MAX_LINE = 65536
MAX_PENDING = 1024


class SharedPoolHealth:
    """Health observations shared by all ASICs on one pool connection."""

    def __init__(self, job_timeout, max_rtt, recovery_delay, *, clock=time.monotonic):
        self.job_timeout = job_timeout
        self.max_rtt = max_rtt
        self.recovery_delay = recovery_delay
        self.clock = clock
        self.last_job = None
        self.bad_samples = 0
        self.degraded = False
        self.recovery_started = None

    def job_received(self):
        now = self.clock()
        if self.last_job is not None and now - self.last_job >= self.job_timeout:
            self.mark_degraded()
        self.last_job = now
        if self.degraded and self.recovery_started is None:
            self.recovery_started = now

    def mark_degraded(self):
        self.degraded = True
        self.recovery_started = None

    def submitted(self, outcome, elapsed, error):
        # Stale and duplicate shares do not indicate a broken pool.
        if isinstance(error, list) and error and error[0] in (21, 22):
            return
        if outcome == 'accepted' and elapsed <= self.max_rtt:
            self.bad_samples = 0
        else:
            self.bad_samples += 1
            if self.bad_samples >= 3:
                self.mark_degraded()

    def healthy(self):
        now = self.clock()
        if self.last_job is None:
            return False
        if now - self.last_job >= self.job_timeout:
            self.mark_degraded()
            return False
        if self.degraded and self.recovery_started is not None:
            if now - self.recovery_started >= self.recovery_delay:
                self.degraded = False
                self.bad_samples = 0
        return not self.degraded


class SharedUpstreamGroup:
    """One authenticated upstream and non-overlapping per-miner nonce leases.

    This is transport ownership, not a Stratum router: the caller must broadcast
    jobs, validate proofs and distinguish local acks from pool confirmations.
    """

    def __init__(self, key, connection, partitioner):
        if not isinstance(key, AggregationKey) or not isinstance(partitioner, NoncePartitioner):
            raise ValueError('invalid shared upstream context')
        if (connection.closed or not connection.subscribed or not connection.authorized or
                connection.extranonce1 != partitioner.upstream_extranonce1 or
                connection.extranonce2_size != partitioner.upstream_extranonce2_size or
                connection.version_mask != key.version_mask):
            raise ValueError('upstream is not authenticated or nonce context differs')
        self.key = key
        self.connection = connection
        self.partitioner = partitioner
        self.subscribers = {}
        self._upstream_notification = connection.on_notification
        self._upstream_disconnect = connection.on_disconnect
        connection.on_notification = self._notification
        connection.on_disconnect = self._disconnect

    async def _disconnect(self, connection):
        subscribers = list(self.subscribers.items())
        for session_id, _ in subscribers:
            self.unsubscribe(session_id)
        await asyncio.gather(*(entry[2]() for _, entry in subscribers), return_exceptions=True)
        await self._upstream_disconnect(connection)

    async def _notification(self, connection, packet):
        # The original handler owns upstream state. Each subscriber has its
        # own bounded queue so a slow ASIC cannot stall the pool reader.
        metrics = getattr(connection, 'metrics', None)
        if metrics is not None and self.key.tls_policy != 'release-fee' and packet.get('method') in (
                'mining.notify', 'mining.set_difficulty', 'mining.set_version_mask'):
            # Compare canonical Stratum payloads, excluding TCP/TLS framing.
            # A direct connection would receive one copy per subscribed ASIC.
            size = len(encode_packet(packet))
            metrics.wan_shared_broadcast_bytes += size
            metrics.wan_direct_broadcast_bytes += size * len(self.subscribers)
        await self._upstream_notification(connection, packet)
        for session_id, (queue, task, on_failure) in list(self.subscribers.items()):
            try:
                queue.put_nowait(copy.deepcopy(packet))
            except asyncio.QueueFull:
                self.unsubscribe(session_id)
                asyncio.create_task(on_failure())

    def subscribe(self, session_id, on_notification, on_failure, *, queue_size=64):
        if session_id in self.subscribers or not callable(on_notification) or not callable(on_failure):
            raise ValueError('invalid or duplicate subscriber')
        if type(queue_size) is not int or not 1 <= queue_size <= 1024:
            raise ValueError('invalid notification queue size')
        queue = asyncio.Queue(maxsize=queue_size)
        async def consume():
            try:
                while True:
                    await on_notification(await queue.get())
            except asyncio.CancelledError:
                raise
            except Exception:
                self.subscribers.pop(session_id, None)
                self.partitioner.release(session_id)
                await on_failure()
        task = asyncio.create_task(consume())
        self.subscribers[session_id] = (queue, task, on_failure)

    def unsubscribe(self, session_id):
        subscriber = self.subscribers.pop(session_id, None)
        if subscriber:
            subscriber[1].cancel()
        return self.partitioner.release(session_id)

    def allocate(self, session_id):
        if self.connection.closed:
            raise ConnectionError('shared upstream disconnected')
        return self.partitioner.allocate(session_id)

    def release(self, session_id):
        # Prefixes remain consumed for the lifetime of the pool connection.
        return self.unsubscribe(session_id)

    async def close(self):
        tasks = [entry[1] for entry in self.subscribers.values()]
        for session_id in list(self.subscribers):
            self.unsubscribe(session_id)
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.connection.close()


class SharedUpstreamManager:
    """Own a bounded collection of compatible, independently authenticated sockets.

    ``factory(key)`` establishes and authenticates a socket and returns an
    UpstreamConnection and its nonce partitioner. Only one socket can be
    opening per key. Other keys and existing groups can make progress meanwhile.
    The owner releases leases after downstream requests have finished.
    """

    def __init__(self, factory, *, max_groups=128):
        if not callable(factory) or type(max_groups) is not int or max_groups < 1:
            raise ValueError('invalid shared upstream manager configuration')
        self.factory = factory
        self.max_groups = max_groups
        self.groups = []
        self.opening = {}
        self.closed = False

    async def acquire(self, key, session_id):
        if not isinstance(key, AggregationKey):
            raise ValueError('invalid aggregation key')
        # All bookkeeping runs on the owning event loop without suspension.
        # Reserve capacity before starting network I/O.
        while True:
            if self.closed:
                raise RuntimeError('shared upstream manager is closed')
            self.groups[:] = [group for group in self.groups if not group.connection.closed]
            for group in self.groups:
                if group.key == key and not group.connection.closed:
                    try:
                        return group, group.allocate(session_id)
                    except InsufficientNonceSpace:
                        pass
            opening = self.opening.get(key)
            if opening is not None:
                await opening.wait()
                continue
            if len(self.groups) + len(self.opening) >= self.max_groups:
                raise InsufficientNonceSpace('shared upstream group limit reached')
            opening = self.opening[key] = asyncio.Event()
            break
        try:
            connection, partitioner = await self.factory(key)
            try:
                if self.closed:
                    raise RuntimeError('shared upstream manager is closed')
                group = SharedUpstreamGroup(key, connection, partitioner)
                lease = group.allocate(session_id)
            except BaseException:
                await connection.close()
                raise
            self.groups.append(group)
            return group, lease
        finally:
            del self.opening[key]
            opening.set()

    async def release(self, group, session_id):
        group.release(session_id)
        if group not in self.groups or group.partitioner.active_count:
            return
        self.groups.remove(group)
        await group.close()

    async def close(self):
        self.closed = True
        groups, self.groups = self.groups, []
        await asyncio.gather(*(group.close() for group in groups),
                             *(opening.wait() for opening in self.opening.values()),
                             return_exceptions=True)


def parse_endpoint(host, port=None):
    """Return DNS/IP host, explicit port and TLS intent, without URL credentials."""
    if not isinstance(host, str) or not host or len(host) > 300 or any(c.isspace() for c in host):
        raise ValueError('invalid pool host')
    secure = False
    if '://' in host:
        url = urlsplit(host)
        if (url.scheme not in ('stratum+ssl', 'stratum+tcp') or url.username is not None or
                url.password is not None or url.path not in ('', '/') or url.query or url.fragment):
            raise ValueError('invalid Stratum URL')
        name, embedded = url.hostname, url.port
        if embedded is not None:
            if port is not None and port != embedded:
                raise ValueError('conflicting pool ports')
            port = embedded
        secure = url.scheme == 'stratum+ssl'
    else:
        name = host
    if (not isinstance(name, str) or not 1 <= len(name) <= 253 or
            not re.fullmatch(r'[A-Za-z0-9._:-]+', name) or type(port) is not int or not 1 <= port <= 65535):
        raise ValueError('invalid pool endpoint')
    return name, port, secure


def pool_label(endpoint):
    host, port, _ = parse_endpoint(*endpoint)
    host = host.lower()
    return f'[{host}]:{port}' if ':' in host else f'{host}:{port}'


@lru_cache(maxsize=1)
def default_tls_context():
    return ssl.create_default_context()


class UpstreamLoopError(OSError):
    """An actual resolved destination would connect to our own listener."""


async def open_upstream(endpoint, tls_context, timeout, url_tls_context=None, resolver=None,
                        address_guard=None):
    host, port, secure = parse_endpoint(*endpoint)
    owned = resolver is None
    resolver = resolver or AsyncResolver()
    async def connect():
        context = tls_context or (url_tls_context if secure else None)
        if context is None and secure:
            context = await resolver.tls_context(default_tls_context)
        if context is not None and (not context.check_hostname or context.verify_mode != ssl.CERT_REQUIRED):
            raise ValueError('upstream TLS requires certificate and hostname verification')
        addresses = await resolver.resolve(host,port,timeout)
        last_error = None
        for family,socktype,proto,_,address in addresses:
            # Check the exact numeric destination that will be connected. A
            # hostname preflight alone is vulnerable to TTL-zero DNS rebinding.
            if address_guard is not None and await address_guard((address[0],address[1])):
                raise UpstreamLoopError('upstream loops back to listener')
            sock = socket.socket(family,socktype,proto)
            sock.setblocking(False)
            try:
                await asyncio.get_running_loop().sock_connect(sock,address)
                kwargs = {'sock':sock,'ssl':context}
                if context is not None: kwargs['server_hostname'] = host
                reader,writer = await asyncio.open_connection(**kwargs)
                configure_writer(writer)
                return reader,writer
            except BaseException as exc:
                sock.close()
                if not isinstance(exc,OSError): raise
                last_error = exc
        raise last_error or OSError('no usable upstream address')
    try:
        return await asyncio.wait_for(connect(),timeout)
    finally:
        if owned: await resolver.close()


def encode_packet(value):
    # Keep wire difficulty decimals exact instead of converting them to binary floats.
    def encode(item):
        if isinstance(item, Decimal):
            if not item.is_finite():
                raise ProtocolError("non-finite JSON number")
            return str(item)
        if isinstance(item, dict):
            return "{" + ",".join(json.dumps(str(k)) + ":" + encode(v) for k, v in item.items()) + "}"
        if isinstance(item, (list, tuple)):
            return "[" + ",".join(encode(v) for v in item) + "]"
        return json.dumps(item, allow_nan=False, ensure_ascii=True, separators=(",", ":"))
    data = encode(value).encode() + b"\n"
    if len(data) > MAX_LINE:
        raise ProtocolError("outgoing Stratum line too long")
    return data


async def read_packet(reader):
    """Compatibility helper; production sessions own explicit role-specific readers."""
    framing = getattr(reader, '_chefas_proxy_packet_reader', None)
    if framing is None:
        framing = PacketReader(reader, max_bytes=MAX_LINE)
        reader._chefas_proxy_packet_reader = framing
    return await framing.read_packet()


class UpstreamConnection:
    """Own IDs, pending futures and bounded writes; no miner IDs cross channels."""

    def __init__(self, reader, writer, target, on_notification, on_disconnect, timeout=10,
                 miner_writer=None, miner_reader=None, metrics=None):
        self.reader, self.writer, self.target = reader, writer, target
        self.packets = PacketReader(reader, max_bytes=MAX_LINE, role="pool", metrics=metrics)
        self.on_notification, self.on_disconnect = on_notification, on_disconnect
        self.timeout = timeout
        self.miner_writer, self.miner_reader, self.metrics = miner_writer,miner_reader,metrics
        configure_writer(writer)
        self.pending = {}
        self.sequence = 0
        self.write_lock = asyncio.Lock()
        self.closed = False
        self.extranonce1 = None
        self.extranonce2_size = None
        self.difficulty = None
        self.job = None
        self.version_mask = None
        self.subscribed = False
        self.authorized = False
        self.ready = asyncio.Event()
        self.task = asyncio.create_task(self._read())

    async def configure_version_rolling(self, mask, minimum_bits=0):
        params = [['version-rolling'], {'version-rolling.mask': mask,
                                        'version-rolling.min-bit-count': minimum_bits}]
        self.version_mask = negotiated_mask(params, await self.request('mining.configure', params))
        return self.version_mask

    async def request(self, method, params):
        if self.closed or len(self.pending) >= MAX_PENDING:
            raise ConnectionError("upstream unavailable or request limit reached")
        self.sequence += 1
        ident = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[ident] = future
        try:
            async with self.write_lock:
                write_bounded(self.writer, encode_packet({"id":ident,"method":method,"params":params}),
                              self.metrics, traffic='wan_tx_bytes')
                await drain_bounded(self.writer,self.timeout,self.miner_writer,self.miner_reader,self.metrics)
            return await asyncio.wait_for(future, self.timeout)
        finally:
            self.pending.pop(ident, None)

    async def _read(self):
        try:
            while packet := await self.packets.read_packet():
                if "method" in packet:
                    await self.on_notification(self, packet)
                else:
                    ident = packet.get("id")
                    if type(ident) is int:
                        future = self.pending.get(ident)
                        if future is not None and not future.done():
                            future.set_result(packet)
        except (OSError, asyncio.TimeoutError, ProtocolError, ValueError, TypeError):
            pass
        finally:
            self.closed = True
            self.ready.set()
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(ConnectionError("upstream disconnected"))
            self.writer.close()
            await self.on_disconnect(self)

    async def close(self):
        self.closed = True
        self.writer.close()
        if self.task is not asyncio.current_task():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        try:
            await asyncio.wait_for(self.writer.wait_closed(), 3)
        except (OSError, asyncio.TimeoutError):
            pass


async def connect_upstream(primary, backup, tls_context, timeout, is_self_route, metrics, url_tls_context=None, resolver=None):
    endpoints = [primary] + ([backup] if backup else [])
    for index, endpoint in enumerate(endpoints):
        try:
            if await is_self_route(endpoint):
                metrics.blocked_loops += 1
                metrics.upstream_failures += 1
                continue
            reader, writer = await open_upstream(endpoint, tls_context, timeout, url_tls_context,resolver,
                                                 address_guard=is_self_route)
            if index:
                metrics.backup_connections += 1
            return reader, writer, index
        except UpstreamLoopError:
            metrics.blocked_loops += 1
            metrics.upstream_failures += 1
        except (OSError, asyncio.TimeoutError, ssl.SSLError):
            metrics.upstream_failures += 1
            event(logging.WARNING, "upstream_connect_failed", target="primary" if index == 0 else "backup")
    return None, None, None

# Configuration snapshots are immutable so a live session never mixes credentials.
from dataclasses import dataclass, field


@dataclass(frozen=True)
class PoolRoute:
    host: str
    port: int
    worker: str = ''
    password: str = field(default='x', repr=False)

    @classmethod
    def parse(cls, value, previous=None):
        if not isinstance(value, dict) or set(value) - {'host', 'port', 'worker', 'password'}:
            raise ValueError('invalid pool route')
        host, port = value.get('host'), value.get('port')
        worker = value.get('worker', previous.worker if previous else '')
        password = value.get('password', previous.password if previous else 'x')
        _, port, _ = parse_endpoint(host, port)
        if not isinstance(worker, str) or len(worker) > 128 or any(ord(c) < 32 for c in worker):
            raise ValueError('invalid worker')
        if not isinstance(password, str) or len(password) > 1024:
            raise ValueError('invalid password')
        return cls(host, port, worker, password)

    def export(self, secrets=False):
        result = {'host': self.host, 'port': self.port, 'worker': self.worker}
        if secrets:
            result['password'] = self.password
        else:
            result['password_configured'] = bool(self.password)
        return result


def current_pool_routes(relay):
    user = PoolRoute(relay.host, relay.port, getattr(relay, 'user_worker', ''), getattr(relay, 'user_password', 'x'))
    fee = PoolRoute(*relay.fee, getattr(relay, 'fee_worker', ''), getattr(relay, 'fee_password', 'x')) if relay.fee else None
    return user, fee


def apply_pool_routes(relay, user, fee):
    # No awaits: all listener callbacks observe one complete generation.
    relay.host, relay.port, relay.user_worker, relay.user_password = user.host, user.port, user.worker, user.password
    relay.fee = (fee.host, fee.port) if fee else None
    relay.fee_worker, relay.fee_password = (fee.worker, fee.password) if fee else ('', 'x')
    relay.route_generation = getattr(relay, 'route_generation', 0) + 1
    relay.metrics._devfee_enabled = fee is not None
