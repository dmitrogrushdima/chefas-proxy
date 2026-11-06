"""Bounded c-ares DNS and per-stream write flow control."""
import asyncio
from collections import OrderedDict
import ipaddress
import socket
import time

import aiodns

WRITE_HIGH = 65536
WRITE_LOW = 16384
WRITE_HARD = 262144
_STREAM_DRAIN = asyncio.StreamWriter.drain
_DRAIN_HELPER = asyncio.streams.FlowControlMixin._drain_helper


class AsyncResolver:
    """Loop-local single-flight DNS; no system getaddrinfo executor fallback."""
    def __init__(self, ttl=60, negative_ttl=2, capacity=256, max_pending=16, query_timeout=5, lookup=None):
        self.ttl, self.negative_ttl, self.capacity = ttl, negative_ttl, capacity
        self.max_pending, self.query_timeout = max_pending, query_timeout
        self.lookup = lookup
        self.cache, self.pending = OrderedDict(), {}
        self.backend = None
        self.closed = False
        self.tls_task = None

    async def _lookup(self, host, port):
        if self.lookup is not None:
            return await self.lookup(host, port)
        if self.backend is None:
            self.backend = aiodns.DNSResolver(timeout=self.query_timeout, tries=1)
        try:
            result = await self.backend.getaddrinfo(host, family=socket.AF_UNSPEC, port=port,
                type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
        except aiodns.error.DNSError as exc:
            raise OSError('asynchronous DNS resolution failed') from exc
        infos = []
        for node in result.nodes[:16]:
            address = node.addr
            host_ip = address[0].decode('ascii') if isinstance(address[0],bytes) else address[0]
            infos.append((node.family,socket.SOCK_STREAM,socket.IPPROTO_TCP,'',(host_ip,*address[1:])))
        return infos, min((node.ttl for node in result.nodes), default=0)

    async def _query(self, key):
        try:
            response = await asyncio.wait_for(self._lookup(*key), self.query_timeout)
            ttl = self.ttl
            if isinstance(response,tuple) and len(response) == 2 and isinstance(response[1],(int,float)):
                response, ttl = response[0], min(self.ttl,max(0,response[1]))
            result = tuple(response)[:16]
            if not result:
                raise OSError('empty DNS response')
            self.cache[key] = (time.monotonic()+ttl, result)
            return result
        except (OSError, asyncio.TimeoutError):
            self.cache[key] = (time.monotonic()+self.negative_ttl, None)
            raise
        finally:
            self.pending.pop(key, None)
            while len(self.cache) > self.capacity:
                self.cache.popitem(last=False)

    async def resolve(self, host, port, timeout):
        if self.closed:
            raise OSError('resolver is closed')
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
            addr = (str(address),port,0,0) if address.version == 6 else (str(address),port)
            return ((family,socket.SOCK_STREAM,socket.IPPROTO_TCP,'',addr),)
        key = (host.lower(),port)
        cached = self.cache.get(key)
        if cached is not None and cached[0] > time.monotonic():
            self.cache.move_to_end(key)
            if cached[1] is None:
                raise OSError('cached DNS failure')
            return cached[1]
        task = self.pending.get(key)
        if task is None:
            if len(self.pending) >= self.max_pending:
                raise OSError('DNS concurrency limit reached')
            task = asyncio.create_task(self._query(key))
            self.pending[key] = task
            task.add_done_callback(lambda t: None if t.cancelled() else t.exception())
        return await asyncio.wait_for(asyncio.shield(task), timeout)

    async def tls_context(self, factory):
        if self.tls_task is None:
            self.tls_task = asyncio.create_task(asyncio.to_thread(factory))
            self.tls_task.add_done_callback(lambda t: None if t.cancelled() else t.exception())
        return await asyncio.shield(self.tls_task)

    async def close(self):
        self.closed = True
        tasks = list(self.pending.values())
        for task in tasks: task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.pending.clear()
        self.cache.clear()
        if self.backend is not None:
            await self.backend.close()
            self.backend = None
        if self.tls_task is not None:
            await asyncio.gather(self.tls_task, return_exceptions=True)


def configure_writer(writer):
    transport = getattr(writer,'transport',None)
    if transport is not None:
        transport.set_write_buffer_limits(high=WRITE_HIGH, low=WRITE_LOW)


def buffer_size(writer):
    transport = getattr(writer,'transport',None)
    value = transport.get_write_buffer_size() if transport is not None else 0
    return value if type(value) is int else 0


def write_bounded(writer, data, metrics=None):
    if buffer_size(writer) + len(data) > WRITE_HARD:
        if metrics is not None: metrics.write_buffer_rejected += 1
        writer.close()
        transport = getattr(writer,'transport',None)
        if transport is not None: transport.abort()
        raise ConnectionError('write buffer limit exceeded')
    writer.write(data)


async def drain_bounded(writer, timeout, source_writer=None, source_reader=None, metrics=None):
    # The standard StreamWriter has no suspension point when its protocol is
    # unpaused and its transport is open. Await it inline to preserve exception
    # checks without allocating a Task/Future/timer for every tiny share packet.
    # Restrict this to the concrete stdlib class: custom drains may block even
    # when a test double or subclass advertises an unpaused protocol.
    if (type(writer) is asyncio.StreamWriter and type(writer._protocol) is asyncio.StreamReaderProtocol and
            getattr(writer.drain,'__func__',None) is _STREAM_DRAIN and
            getattr(writer._protocol._drain_helper,'__func__',None) is _DRAIN_HELPER and
            writer._protocol._paused is False and not writer.is_closing()):
        await writer.drain()
        return
    source = getattr(source_writer,'transport',None)
    pause_state = None
    try:
        if buffer_size(writer) >= WRITE_HIGH and source is not None:
            # User and fee RPC writers can simultaneously backpressure one
            # ASIC. Every stalled drain owns a reference, including drains that
            # arrive after another one has already paused the source.
            pause_state = getattr(source_writer,'_chefas_proxy_read_pressure',None)
            if pause_state is None:
                pause_state = {'users':0,'owned':False}
                source_writer._chefas_proxy_read_pressure = pause_state
            pause_state['users'] += 1
            if source.is_reading():
                source.pause_reading()
                pause_state['owned'] = True
                if metrics is not None: metrics.backpressure_pauses += 1
        # asyncio.wait_for on Python 3.10 can swallow an outer cancellation
        # when its child finishes in the same loop turn. Keep cancellation
        # authoritative for long-lived reader/writer pumps.
        draining = asyncio.ensure_future(writer.drain())
        try:
            done,_ = await asyncio.wait((draining,),timeout=timeout)
            if not done:
                raise asyncio.TimeoutError()
            draining.result()
        finally:
            if not draining.done(): draining.cancel()
            await asyncio.gather(draining,return_exceptions=True)
    except asyncio.TimeoutError:
        if metrics is not None: metrics.write_timeouts += 1
        writer.close()
        transport = getattr(writer,'transport',None)
        if transport is not None: transport.abort()
        raise
    finally:
        if pause_state is not None:
            pause_state['users'] -= 1
            if pause_state['users'] == 0:
                if (pause_state['owned'] and not source.is_closing() and
                        not getattr(source_reader,'_paused',False)):
                    source.resume_reading()
                pause_state['owned'] = False
