"""Experimental transparent relay and per-connection lifecycle."""
import asyncio
import errno
import ipaddress
import logging
import math
import socket
import ssl
import time

from observer import Metrics, Observer, WorkerRegistry
from collections import deque
from datetime import datetime, timezone
from router import copy_stream
from upstream_manager import connect_upstream, parse_endpoint, pool_label
from devfee_scheduler import AcceptedWorkLedger, FeeConnectionGate, FairnessAllocator, StableFeeSlots
from pilot_logging import event
# Public framing types are re-exported here for session-level integrations.
from stratum_codec import PacketReader, StratumDecoder
from hardware_compatibility import CompatibilityRegistry
from network_io import AsyncResolver, configure_writer

LOG = logging.getLogger("mining_proxy")
HALF_CLOSE_GRACE = 3.0


def enable_tcp_keepalive(writer):
    """Best effort kernel probes; the application deadline remains authoritative."""
    sock = writer.get_extra_info("socket")
    if sock is None:
        return False
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except (OSError, AttributeError):
        return False
    for name, value in (("TCP_KEEPIDLE", 60), ("TCP_KEEPINTVL", 15), ("TCP_KEEPCNT", 3)):
        option = getattr(socket, name, None)
        if option is not None:
            try:
                sock.setsockopt(socket.IPPROTO_TCP, option, value)
            except OSError:
                pass
    return True


class Relay:
    def __init__(self, host, port, *, tls=False, limit=512, timeout=10, backup=None, tls_context=None, allowed_networks=(), idle_timeout=0, miner_timeout=600, handshake_timeout=30, max_miner_message=4096):
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
        if not isinstance(miner_timeout, (int,float)) or not math.isfinite(miner_timeout) or (miner_timeout != 0 and not .1 <= miner_timeout <= 86400):
            raise ValueError("miner timeout must be 0 or .1..86400")
        if not isinstance(handshake_timeout, (int,float)) or not math.isfinite(handshake_timeout) or not .1 <= handshake_timeout <= 300:
            raise ValueError("handshake timeout must be .1..300")
        if type(max_miner_message) is not int or not 512 <= max_miner_message <= 65536:
            raise ValueError("miner frame limit must be 512..65536")
        self.miner_timeout, self.handshake_timeout = miner_timeout, handshake_timeout
        self.max_miner_message = max_miner_message
        self.active = 0
        self.tasks = set()
        self.metrics = Metrics()
        self.backup = backup
        self.sessions = {}
        self.next_session_id = 0
        self.listeners = []
        self.allowed_networks = tuple(ipaddress.ip_network(network, strict=True) for network in allowed_networks)
        self.active_router = False
        self.tp_mode = False
        self.shared_upstreams = None
        self.fee = None
        self.fee_jitter_seconds = 180
        self.fee_connection_gate = FeeConnectionGate()
        self.job_ttl = 900
        self.job_gc_interval = 60
        self.url_tls_context = None
        self.compatibility = CompatibilityRegistry()
        self.resolver = AsyncResolver(query_timeout=timeout)
        self.hot_switch_mode = 'auto'
        self.asic_extranonce2_min = 0
        self.asic_extranonce2_max = 4
        self.ledger = AcceptedWorkLedger()
        self.fee_allocator = FairnessAllocator(self.ledger, max_active=limit, max_pending=limit)
        self.fee_slots = StableFeeSlots(limit)
        self.fee_worker_template = None
        self.events = deque(maxlen=500)
        self.diagnostic_events = deque(maxlen=100)
        self.workers = WorkerRegistry(max_workers=max(512, limit))
        self.blacklist = ()
        self.route_configs = {}
        if tls_context is not None and not tls:
            raise ValueError("TLS context provided while TLS is disabled")
        self.tls_context = (tls_context or ssl.create_default_context()) if tls else None
        if self.tls_context and (not self.tls_context.check_hostname or
                                 self.tls_context.verify_mode != ssl.CERT_REQUIRED):
            raise ValueError("TLS requires hostname and certificate verification")

    async def handle(self, reader, writer, route_id="main"):
        peer = writer.get_extra_info("peername")
        allowed = self.route_configs.get(route_id, {}).get("allowed", ())
        if not self.peer_allowed(peer) or (allowed and not any(
                ipaddress.ip_address(WorkerRegistry.normalize_ip(peer[0])) in network for network in allowed)):
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
        if not enable_tcp_keepalive(writer):
            self.metrics.keepalive_unavailable += 1
        configure_writer(writer)
        self.active += 1
        self.metrics.active += 1
        self.metrics.connections += 1
        self.next_session_id += 1
        session_id = self.next_session_id
        session_metrics = Metrics(parent=self.metrics)
        session = {"id": session_id, "state": "connecting", "worker": None,
                   "pool": None, "started": time.monotonic(), "metrics": session_metrics,
                   "writer": writer, "ip": writer.get_extra_info("peername")[0],
                   "route_id": route_id, "protocol_started": not self.active_router or self.tp_mode}
        session["endpoint"] = self.route_configs.get(route_id, {}).get("endpoint", (self.host, self.port))
        self.sessions[session_id] = session
        if self.active_router and not self.tp_mode:
            self.observe_connection(session, "tcp_connected")
        if not self.active_router or self.tp_mode:
            event(logging.INFO, "session_started", session_id=session_id, active_router=False)

        def authorized(name):
            session["worker"] = "".join(c if c.isprintable() else "?" for c in name)
            self.workers.authorize(session_id, name, session["ip"])

        def share_observed(name, outcome, latency, stale):
            if name is not None:
                self.workers.record(session_id, name, outcome, rtt_ms=latency, stale=stale)
            if 'upstream_label' in session:
                route = 'backup' if session['pool'] == 'backup' else 'user'
                self.metrics._pool_telemetry.observe(
                    route, session['upstream_label'], outcome, None if latency is None else latency / 1000)

        observer = Observer(session_metrics, on_authorized=authorized, on_share=share_observed)
        session["observer"] = observer
        session["tp_mode"] = self.tp_mode
        wire_observer = None if self.tp_mode else observer
        task = asyncio.current_task()
        self.tasks.add(task)
        upstream = None
        pumps = []
        try:
            if self.active_router and not session["tp_mode"]:
                from router import AggregateSession, StatefulRouter
                router = AggregateSession if self.shared_upstreams else StatefulRouter
                await router(self, reader, writer, session).run()
                return
            backup = self.backup
            remote_reader, upstream, index = await connect_upstream(
                session["endpoint"], backup, self.tls_context, self.timeout,
                self.is_self_route, self.metrics, self.url_tls_context, self.resolver
            )
            if upstream is None:
                return
            session["state"] = "connected"
            session["pool"] = "primary" if index == 0 else "backup"
            session['upstream_label'] = pool_label(session['endpoint'] if index == 0 else backup)

            last_activity = time.monotonic()
            last_miner_activity = last_activity

            def mark_activity():
                nonlocal last_activity
                last_activity = time.monotonic()

            def mark_miner_activity():
                nonlocal last_miner_activity
                last_miner_activity = time.monotonic()
                mark_activity()

            pumps = [asyncio.create_task(copy_stream(reader,upstream,0,wire_observer,self.metrics,mark_miner_activity,self.timeout,writer)),
                     asyncio.create_task(copy_stream(remote_reader,writer,1,wire_observer,self.metrics,mark_activity,self.timeout,upstream))]
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
            # Reuse the pump wait instead of allocating another long-lived Task
            # per transparent connection. Inbound bytes renew only this deadline.
            miner_expired = False
            while True:
                remaining = (max(0, self.miner_timeout - (time.monotonic() - last_miner_activity))
                             if self.miner_timeout else None)
                done, _ = await asyncio.wait(pumps, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
                if done:
                    break
                if time.monotonic() - last_miner_activity >= self.miner_timeout:
                    miner_expired = True
                    break
            for finished in done:
                finished.result()
            # Plain TCP can half-close: forward the client's EOF and allow a bounded
            # window for final pool responses. TLS transports cannot write EOF.
            if pumps[0] in done and pumps[1] not in done and (not watchdog or watchdog not in done) and not miner_expired and upstream.can_write_eof():
                upstream.write_eof()
                waiting = [pumps[1]] + ([watchdog] if watchdog else [])
                later, _ = await asyncio.wait(waiting, timeout=HALF_CLOSE_GRACE,
                                               return_when=asyncio.FIRST_COMPLETED)
                for finished in later:
                    finished.result()
                done |= later
            if miner_expired:
                self.metrics.zombie_reaped += 1
            elif watchdog and watchdog in done:
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
                if "diagnostic" in session:
                    self.observe_connection(session, "disconnected")
                self.workers.disconnect(session_id)
                self.sessions.pop(session_id, None)
                self.tasks.discard(task)
                if session["protocol_started"]:
                    event(logging.INFO, "session_closed", session_id=session_id,
                          accepted=session_metrics.accepted, rejected=session_metrics.rejected,
                          unknown=session_metrics.unmatched)

    def set_listeners(self, *servers):
        self.listeners = [(item.family, item.getsockname()) for server in servers for item in server.sockets]

    def peer_allowed(self, peer):
        if not self.allowed_networks and not self.blacklist:
            return True
        try:
            address = ipaddress.ip_address(peer[0].split("%", 1)[0])
            candidates = (address, address.ipv4_mapped) if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped else (address,)
            if any(candidate.version == network.version and candidate in network
                   for candidate in candidates for network in self.blacklist):
                return False
            if not self.allowed_networks:
                return True
            return any(candidate.version == network.version and candidate in network
                       for candidate in candidates for network in self.allowed_networks)
        except (ValueError, TypeError, IndexError, AttributeError):
            return False

    async def is_self_route(self, endpoint):
        host, port, _ = parse_endpoint(*endpoint)
        endpoint = (host, port)
        relevant = [(family, address) for family, address in self.listeners if address[1] == endpoint[1]]
        if not relevant:
            return False
        # Reserved localhost must never require a DNS executor round-trip to
        # reject a loop into our own local/wildcard listener.
        if host.lower() == 'localhost' and any(
                ipaddress.ip_address(address[0].split('%',1)[0]).is_loopback or
                ipaddress.ip_address(address[0].split('%',1)[0]).is_unspecified
                for _,address in relevant):
            return True
        loop = asyncio.get_running_loop()
        addresses = await self.resolver.resolve(endpoint[0],endpoint[1],self.timeout)
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
                 "protocol_observation": dict(item["observer"].protocol),
                 "ip": item["ip"], "route_id": item["route_id"],
                 "pool_response_ms": (item["metrics"].share_rtt_total_ms / item["metrics"].share_rtt_samples
                                      if item["metrics"].share_rtt_samples else None),
                 "diagnostic": dict(item.get("diagnostic", {})),
                 "test_fee_percent": getattr(item.get("fee_test_router"), "test_fee_percent", None),
                 "asic_ping_ms": None, "scope": "TCP session; multiple workers may share a connection"}
                for item in self.sessions.values()]

    def live_snapshot(self):
        safe_keys = ("id", "state", "worker", "pool", "ip", "route_id", "duration_seconds",
                     "submitted", "accepted", "rejected", "unmatched", "pending", "diagnostic", "test_fee_percent")
        return {"generated_at_utc": datetime.now(timezone.utc).isoformat(),
                "metrics": self.metrics.snapshot(), "status": self.status_snapshot(),
                "sessions": [{key: item[key] for key in safe_keys} for item in self.session_snapshot()],
                "registry": self.workers.snapshot(), "events": list(self.events),
                "diagnostic_events": list(self.diagnostic_events),
                "stream": {"interval_seconds": 1, "scope": "local process; latest state; no replay"}}

    def observe_connection(self, session, stage, route=None, pool=None, *, worker=None):
        state = session.setdefault("diagnostic", {})
        route = route if route is not None else state.get("route")
        pool = pool if pool is not None else state.get("pool")
        if isinstance(worker, str):
            state["worker"] = "".join(c if c.isprintable() else "?" for c in worker[:128])
        previous = state.get("stage")
        same_route = (route, pool) == (state.get("route"), state.get("pool"))
        if stage == "job_sent" and same_route and previous == "share_accepted":
            return
        if stage == "share_accepted" and (not same_route or previous not in ("job_sent", "share_accepted")):
            # A reply to an old job must not mark the new route as working.
            return
        timestamp = datetime.now(timezone.utc).isoformat()
        if stage == "share_accepted":
            state["last_accepted_utc"] = timestamp
            state["last_session_accepted_utc"] = timestamp
        if same_route and stage == previous:
            return
        if not same_route:
            state.pop("last_accepted_utc", None)
        state.update(stage=stage, route=route, pool=pool, changed_at_utc=timestamp)
        self.diagnostic_events.append({
            "timestamp": timestamp, "session_id": session["id"],
            "worker": session["worker"] or state.get("worker"),
            "route": route, "pool": pool, "stage": stage,
        })

    def event(self, session, target, kind="switch"):
        self.events.append({"timestamp": datetime.now(timezone.utc).isoformat(),
                            "session_id": session["id"], "worker": session["worker"],
                            "target": target, "kind": kind,
                            "switched_to_devfee": target == "fee"})
        event(logging.INFO, "session_route", session_id=session["id"], target=target, kind=kind)

    def status_snapshot(self):
        return {"primary": {"host": self.host, "port": self.port,
                            "tls": self.tls or parse_endpoint(self.host,self.port)[2]},
                "devfee_jitter_seconds": self.fee_jitter_seconds,
                "devfee_connect_interval": self.fee_connection_gate.interval,
                "fee_allocation": self.fee_allocator.snapshot(),
                "job_ttl_seconds": self.job_ttl,
                "job_gc_interval_seconds": self.job_gc_interval,
                "miner_timeout_seconds": self.miner_timeout,
                "handshake_timeout_seconds": self.handshake_timeout,
                "max_miner_message_bytes": self.max_miner_message,
                "active_router": self.active_router,
                "tp_mode": self.tp_mode,
                "require_devfee": getattr(self, "require_devfee", False),
                "fee_policy_blocked": getattr(self, "fee_blocked", False),
                "backup": {"host": self.backup[0], "port": self.backup[1],
                           "tls": self.tls or parse_endpoint(*self.backup)[2]} if self.backup else None,
                "tls": self.tls, "limit": self.limit,
                "listeners": [str(item[1][1]) for item in self.listeners],
                "tenant_fee": {name: profile["windows"].allocator.ledger.snapshot()
                               for name, profile in self.route_configs.items() if "windows" in profile}}

    def prometheus_snapshot(self):
        workers = sum(session['worker'] is not None for session in self.sessions.values())
        sockets = (sum(not group.connection.closed for group in self.shared_upstreams.groups)
                   if self.shared_upstreams is not None else 0)
        return self.metrics._pool_telemetry.render(self.metrics, workers, sockets)

    async def shutdown(self):
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.shared_upstreams is not None:
            await self.shared_upstreams.close()
        await self.resolver.close()

    async def reconcile_idle_fee_sessions(self):
        """Revoke idle fee grants after another miner repays the shared debt.

        One fleet timer avoids another permanent timer per ASIC. Each writer is
        independently scheduled, so a stalled ASIC cannot hold up its peers.
        """
        if self.shared_upstreams is not None:
            return
        cursor = 0
        while True:
            await asyncio.sleep(.5)
            sessions = tuple(self.sessions.values())
            for session in sessions:
                router = session.get('router')
                if (router and router.current is not None and router.current.target == 'fee'
                        and not session.get('pending',0) and not router.stopping):
                    router.schedule_reconcile()
            # Round-robin bounded admission work; accepted user submissions also
            # reconcile directly. No permanent polling task per miner is needed.
            if sessions and self.ledger.should_fee():
                start = cursor % len(sessions)
                count = min(32,len(sessions))
                for offset in range(count):
                    session = sessions[(start+offset)%len(sessions)]
                    router = session.get('router')
                    if (router and router.auth and router.current is not None
                            and router.current.target != 'fee' and not session.get('pending',0)
                            and not router.stopping):
                        router.schedule_reconcile()
                cursor = start + count
