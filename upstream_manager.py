"""Upstream transports: legacy selection and bounded stateful RPC channels."""
import asyncio
import json
import ssl
from decimal import Decimal

MAX_LINE = 65536
MAX_PENDING = 1024


class ProtocolError(Exception):
    """Untrusted peer sent an invalid or unsupported protocol state."""


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
    try:
        line = await reader.readuntil(b"\n")
    except asyncio.IncompleteReadError as exc:
        if not exc.partial:
            return None
        # A partial frame must never be executed as a complete JSON-RPC request.
        raise ProtocolError("incomplete Stratum line") from exc
    except (asyncio.LimitOverrunError, ValueError) as exc:
        raise ProtocolError("Stratum line too long") from exc
    if len(line) > MAX_LINE:
        raise ProtocolError("Stratum line too long")
    try:
        packet = json.loads(line, parse_float=Decimal,
                            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ProtocolError("invalid Stratum JSON") from exc
    if not isinstance(packet, dict):
        raise ProtocolError("JSON-RPC object required")
    return packet


class UpstreamConnection:
    """Own IDs, pending futures and bounded writes; no miner IDs cross channels."""

    def __init__(self, reader, writer, target, on_notification, on_disconnect, timeout=10):
        self.reader, self.writer, self.target = reader, writer, target
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
            while packet := await read_packet(self.reader):
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
    return None, None, None
