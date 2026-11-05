"""Experimental transparent relay and per-connection lifecycle."""
import asyncio
import errno
import ipaddress
import logging
import math
import socket
import ssl
import time

from observer import Metrics, Observer
from router import copy_stream
from upstream_manager import connect_upstream
from devfee_scheduler import AcceptedWorkLedger

LOG = logging.getLogger("mining_proxy")
HALF_CLOSE_GRACE = 3.0


class Relay:
    def __init__(self, host, port, *, tls=False, limit=512, timeout=10, backup=None, tls_context=None, allowed_networks=(), idle_timeout=0):
        if not host or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("invalid primary pool")
        if not isinstance(limit, int) or not 1 <= limit <= 10000:
            raise ValueError("connection limit must be 1..10000")
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0.1 <= timeout <= 120:
            raise ValueError("connection timeout must be 0.1..120 seconds")
        if not isinstance(idle_timeout, (int, float)) or not math.isfinite(idle_timeout) or (idle_timeout != 0 and not 0.1 <= idle_timeout <= 86400):
            raise ValueError("idle timeout must be 0 or 0.1..86400 seconds")
        if backup and (not backup[0] or not isinstance(backup[1], int) or not 1 <= backup[1] <= 65535):
            raise ValueError("invalid backup pool")
        self.host, self.port, self.tls = host, port, tls
        self.limit, self.timeout = limit, timeout
        self.idle_timeout = idle_timeout
        self.active = 0
        self.tasks = set()
        self.metrics = Metrics()
        self.backup = backup
        self.sessions = {}
        self.next_session_id = 0
        self.listeners = []
        self.allowed_networks = tuple(ipaddress.ip_network(network, strict=True) for network in allowed_networks)
        self.active_router = False
        self.fee = None
        self.fee_owner = None
        self.ledger = AcceptedWorkLedger()
        if tls_context is not None and not tls:
            raise ValueError("TLS context provided while TLS is disabled")
        self.tls_context = (tls_context or ssl.create_default_context()) if tls else None
        if self.tls_context and (not self.tls_context.check_hostname or
                                 self.tls_context.verify_mode != ssl.CERT_REQUIRED):
            raise ValueError("TLS requires hostname and certificate verification")

    async def handle(self, reader, writer):
        if not self.peer_allowed(writer.get_extra_info("peername")):
            self.metrics.refused_network += 1
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
            return
        if self.active >= self.limit:
            self.metrics.refused_capacity += 1
            writer.close()
            await writer.wait_closed()
            return
        self.active += 1
        self.metrics.active += 1
        self.metrics.connections += 1
        self.next_session_id += 1
        session_id = self.next_session_id
        session_metrics = Metrics(parent=self.metrics)
        session = {"id": session_id, "state": "connecting", "worker": None,
                   "pool": None, "started": time.monotonic(), "metrics": session_metrics}
        self.sessions[session_id] = session

        def authorized(name):
            session["worker"] = "".join(c if c.isprintable() else "?" for c in name)

        observer = Observer(session_metrics, on_authorized=authorized)
        session["observer"] = observer
        task = asyncio.current_task()
        self.tasks.add(task)
        upstream = None
        pumps = []
        try:
            if self.active_router:
                from router import StatefulRouter
                await StatefulRouter(self, reader, writer, session).run()
                return
            remote_reader, upstream, index = await connect_upstream(
                (self.host, self.port), self.backup, self.tls_context, self.timeout,
                self.is_self_route, self.metrics
            )
            if upstream is None:
                return
            session["state"] = "connected"
            session["pool"] = "primary" if index == 0 else "backup"

            last_activity = time.monotonic()

            def mark_activity():
                nonlocal last_activity
                last_activity = time.monotonic()

            pumps = [asyncio.create_task(copy_stream(reader, upstream, 0, observer, self.metrics, mark_activity)),
                     asyncio.create_task(copy_stream(remote_reader, writer, 1, observer, self.metrics, mark_activity))]
            watchdog = None
            if self.idle_timeout:
                async def watch_idle():
                    while True:
                        remaining = self.idle_timeout - (time.monotonic() - last_activity)
                        if remaining <= 0:
                            return
                        await asyncio.sleep(remaining)
                watchdog = asyncio.create_task(watch_idle())
                pumps.append(watchdog)
            done, _ = await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
            for finished in done:
                finished.result()
            # Plain TCP can half-close: forward the client's EOF and allow a bounded
            # window for final pool responses. TLS transports cannot write EOF.
            if pumps[0] in done and pumps[1] not in done and (not watchdog or watchdog not in done) and upstream.can_write_eof():
                upstream.write_eof()
                waiting = [pumps[1]] + ([watchdog] if watchdog else [])
                later, _ = await asyncio.wait(waiting, timeout=HALF_CLOSE_GRACE,
                                               return_when=asyncio.FIRST_COMPLETED)
                for finished in later:
                    finished.result()
                done |= later
            if watchdog and watchdog in done:
                self.metrics.idle_timeouts += 1
            elif pumps[0] in done and pumps[1] in done:
                self.metrics.closed_ambiguous += 1
            elif pumps[0] in done:
                self.metrics.closed_by_miner += 1
            else:
                self.metrics.closed_by_pool += 1
        except (OSError, asyncio.TimeoutError, ssl.SSLError):
            self.metrics.transport_errors += 1
            LOG.info("Connection ended or upstream unavailable")
        finally:
            try:
                for pump in pumps:
                    pump.cancel()
                if pumps:
                    await asyncio.gather(*pumps, return_exceptions=True)
                for stream in (upstream, writer):
                    if stream:
                        stream.close()
                for stream in (upstream, writer):
                    if stream:
                        try:
                            await asyncio.wait_for(stream.wait_closed(), 3)
                        except (OSError, asyncio.TimeoutError, ssl.SSLError):
                            pass
            finally:
                # Counters and registry must recover even if cleanup is cancelled.
                for stream in (upstream, writer):
                    if stream:
                        stream.close()
                self.active -= 1
                self.metrics.active -= 1
                observer.close()
                self.sessions.pop(session_id, None)
                self.tasks.discard(task)

    def set_listeners(self, server):
        self.listeners = [(item.family, item.getsockname()) for item in server.sockets]

    def peer_allowed(self, peer):
        if not self.allowed_networks:
            return True
        try:
            address = ipaddress.ip_address(peer[0].split("%", 1)[0])
            candidates = (address, address.ipv4_mapped) if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped else (address,)
            return any(candidate.version == network.version and candidate in network
                       for candidate in candidates for network in self.allowed_networks)
        except (ValueError, TypeError, IndexError, AttributeError):
            return False

    async def is_self_route(self, endpoint):
        relevant = [(family, address) for family, address in self.listeners if address[1] == endpoint[1]]
        if not relevant:
            return False
        loop = asyncio.get_running_loop()
        addresses = await asyncio.wait_for(loop.getaddrinfo(endpoint[0], endpoint[1], type=socket.SOCK_STREAM), self.timeout)
        for family, _, _, _, remote in addresses:
            for listen_family, local in relevant:
                if family != listen_family:
                    continue
                remote_ip = ipaddress.ip_address(remote[0].split("%", 1)[0])
                local_ip = ipaddress.ip_address(local[0].split("%", 1)[0])
                if remote_ip == local_ip:
                    return True
                if local_ip.is_unspecified:
                    # Binding the target IP establishes whether it belongs to this host.
                    candidate = socket.socket(family, socket.SOCK_STREAM)
                    try:
                        bind_address = (remote[0], 0) if family == socket.AF_INET else (remote[0], 0, 0, remote[3])
                        candidate.bind(bind_address)
                        return True
                    except OSError as exc:
                        if exc.errno not in (errno.EADDRNOTAVAIL, 10049):
                            raise
                    finally:
                        candidate.close()
        return False

    def session_snapshot(self):
        now = time.monotonic()
        return [{"id": item["id"], "state": item["state"], "worker": item["worker"],
                 "pool": item["pool"], "duration_seconds": max(0, int(now - item["started"])),
                 "submitted": item["metrics"].submitted, "accepted": item["metrics"].accepted,
                 "rejected": item["metrics"].rejected, "unmatched": item["metrics"].unmatched,
                 "pending": item.get("pending", item["observer"].pending_shares()),
                 "protocol_observation": dict(item["observer"].protocol)}
                for item in self.sessions.values()]

    def status_snapshot(self):
        return {"primary": {"host": self.host, "port": self.port},
                "active_router": self.active_router,
                "require_devfee": getattr(self, "require_devfee", False),
                "fee_policy_blocked": getattr(self, "fee_blocked", False),
                "backup": {"host": self.backup[0], "port": self.backup[1]} if self.backup else None,
                "tls": self.tls, "limit": self.limit,
                "listeners": [str(item[1][1]) for item in self.listeners]}

    async def shutdown(self):
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
