"""Upstream transports: legacy selection and bounded stateful RPC channels."""
import asyncio
import json
import ssl
import logging
from pilot_logging import event
from decimal import Decimal
from stratum_codec import ProtocolError, PacketReader

MAX_LINE = 65536
MAX_PENDING = 1024


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
    framing = getattr(reader, '_kontur_packet_reader', None)
    if framing is None:
        framing = PacketReader(reader, max_bytes=MAX_LINE)
        reader._kontur_packet_reader = framing
    return await framing.read_packet()


class UpstreamConnection:
    """Own IDs, pending futures and bounded writes; no miner IDs cross channels."""

    def __init__(self, reader, writer, target, on_notification, on_disconnect, timeout=10):
        self.reader, self.writer, self.target = reader, writer, target
        self.packets = PacketReader(reader, max_bytes=MAX_LINE, role="pool")
        self.on_notification, self.on_disconnect = on_notification, on_disconnect
        self.timeout = timeout
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

    async def request(self, method, params):
        if self.closed or len(self.pending) >= MAX_PENDING:
            raise ConnectionError("upstream unavailable or request limit reached")
        self.sequence += 1
        ident = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[ident] = future
        try:
            async with self.write_lock:
                self.writer.write(encode_packet({"id": ident, "method": method, "params": params}))
                await asyncio.wait_for(self.writer.drain(), self.timeout)
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


async def connect_upstream(primary, backup, tls_context, timeout, is_self_route, metrics):
    endpoints = [primary] + ([backup] if backup else [])
    for index, endpoint in enumerate(endpoints):
        try:
            if await is_self_route(endpoint):
                metrics.blocked_loops += 1
                metrics.upstream_failures += 1
                continue
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(*endpoint, ssl=tls_context), timeout
            )
            if index:
                metrics.backup_connections += 1
            return reader, writer, index
        except (OSError, asyncio.TimeoutError, ssl.SSLError):
            metrics.upstream_failures += 1
            event(logging.WARNING, "upstream_connect_failed", target="primary" if index == 0 else "backup")
    return None, None, None

# Configuration snapshots are immutable so a live session never mixes credentials.
from dataclasses import dataclass, field
import re


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
        if not isinstance(host, str) or not 1 <= len(host) <= 253 or not re.fullmatch(r'[A-Za-z0-9._:-]+', host):
            raise ValueError('invalid pool host')
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError('invalid pool port')
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
