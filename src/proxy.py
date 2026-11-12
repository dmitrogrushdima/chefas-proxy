"""CHEFAS Proxy - Bitcoin SHA-256d Stratum V1 pilot."""
import argparse
import asyncio
import logging
import ssl
import json
import time
import math
import socket
import errno
import ipaddress
import os
import sys
import hashlib
import hmac
import subprocess
from logging.handlers import RotatingFileHandler
from pathlib import Path
from observer import Metrics, Observer, WorkAudit
from stratum_session import Relay, HALF_CLOSE_GRACE
from dashboard_server import dashboard_request, ASSET_DIR, ASSETS, DashboardStream
from devfee_scheduler import AcceptedWorkLedger, FairnessAllocator, stable_fee_worker
from dashboard_control import DashboardControl
from history_store import DevFeeJournal, HistoryStore
from secret_store import load_environment, SecretConfigurationError
from pilot_logging import PilotLogging, SafeQueueHandler, event
from shutdown_signals import ShutdownSignals
from upstream_manager import open_upstream, parse_endpoint
from devfee_scheduler import FeeConnectionGate, EventFeeWindows, FeeCapacity
from hardware_compatibility import CompatibilityRegistry

LOG = logging.getLogger("mining_proxy")
DEFAULT_LAN_CIDRS = ("127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")


def release_fee_locked():
    if not getattr(sys, "frozen", False):
        return False
    token = os.environ.get("CHEFAS_PROXY_DEVELOPER_KEY", "")
    digest = hashlib.sha256(token.encode()).hexdigest()
    return not hmac.compare_digest(digest, "bd3911dc32201b4331c3e296c971e5230663eea0d3c1f9c34b4baa7483343d56")


def is_loopback_bind(host):
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        # Unknown hostnames are not treated as loopback for security purposes.
        return False


def fd_limit_warning(limit, soft_limit, upstreams_per_session=1):
    """Conservative estimate, not a guarantee that the OS will accept the load."""
    required = (1 + upstreams_per_session) * limit + 64
    if soft_limit >= 0 and soft_limit < required:
        return (f"File descriptor limit {soft_limit} is below the estimated {required} "
                f"needed for {limit} sessions and overhead")
    return None




def pilot_ipv4_addresses(bind):
    if bind != "0.0.0.0":
        return [bind]
    if sys.platform == "win32":
        command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                   "Get-NetIPAddress -AddressFamily IPv4 | Select-Object -ExpandProperty IPAddress"]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=5,
                                    creationflags=subprocess.CREATE_NO_WINDOW, check=True)
            return sorted({str(ipaddress.IPv4Address(line.strip())) for line in result.stdout.splitlines()
                           if line.strip()})
        except (ValueError, OSError, subprocess.SubprocessError):
            pass
    if sys.platform.startswith("linux"):
        try:
            result = subprocess.run(["ip", "-j", "-4", "address", "show", "up"],
                                    capture_output=True, text=True, timeout=5, check=True)
            return sorted({str(ipaddress.IPv4Address(address["local"]))
                           for device in json.loads(result.stdout)
                           for address in device.get("addr_info", [])
                           if address.get("family") == "inet" and address.get("scope") != "link"})
        except (KeyError, TypeError, ValueError, OSError, subprocess.SubprocessError):
            pass
    return sorted({item[4][0] for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)})


async def pilot_preflight(relay, bind, port):
    try:
        for address in await asyncio.to_thread(pilot_ipv4_addresses, bind):
            if not ipaddress.ip_address(address).is_loopback:
                print(f"[PREFLIGHT] ASIC URL: stratum+tcp://{address}:{port}", flush=True)
    except (OSError, ValueError):
        print("[PREFLIGHT] Local IPv4 discovery failed", flush=True)

    async def probe(name, endpoint):
        if endpoint is None:
            print(f"[PREFLIGHT] {name}: not configured", flush=True)
            return False
        started = time.monotonic()
        transport = "TLS" if relay.tls or parse_endpoint(*endpoint)[2] else "TCP"
        try:
            _, writer = await open_upstream(endpoint, relay.tls_context, min(4, relay.timeout),
                                            relay.url_tls_context, relay.resolver,
                                            address_guard=relay.is_self_route)
            writer.close()
        except (OSError, ValueError, asyncio.TimeoutError):
            print(f"[PREFLIGHT] {name}: {transport} unavailable", flush=True)
            return False
        print(f"[PREFLIGHT] {name}: {transport} OK ({(time.monotonic() - started) * 1000:.0f} ms)", flush=True)
        return True

    _, backup_ok = await asyncio.gather(probe("Primary", (relay.host, relay.port)),
                                        probe("Backup", relay.backup))
    return backup_ok


def load_tenant_profiles(path, primary_port, relay, args):
    if not path:
        return {}
    if not args.aggregate:
        raise ValueError("tenant profiles require aggregation")
    path = Path(path)
    if path.stat().st_size > 65536:
        raise ValueError("tenant profile file too large")
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict) or len(data) > 64 or "main" in data:
        raise ValueError("invalid tenant profiles")
    profiles, ports = {}, {primary_port}
    for name, row in data.items():
        if (not name or len(name) > 64 or not isinstance(row, dict)
                or set(row) - {"listen_port", "pool_host", "pool_port", "backup_host", "backup_port", "worker", "allow_cidr"}):
            raise ValueError("invalid tenant profile")
        port, host = row.get("listen_port"), row.get("pool_host")
        if type(port) is not int or not 1 <= port <= 65535 or port in ports or not isinstance(host, str):
            raise ValueError("invalid tenant listener")
        _, pool_port, _ = parse_endpoint(host, row.get("pool_port"))
        backup_host = row.get("backup_host")
        backup = None
        if backup_host:
            _, backup_port, _ = parse_endpoint(backup_host, row.get("backup_port"))
            backup = backup_host, backup_port
        elif "backup_port" in row:
            raise ValueError("tenant backup port requires a host")
        worker = row.get("worker", "")
        if not isinstance(worker, str) or len(worker) > 128 or any(ord(c) < 32 for c in worker):
            raise ValueError("invalid tenant worker")
        networks = row.get("allow_cidr", [])
        if not isinstance(networks, list) or len(networks) > 128 or any(not isinstance(cidr, str) for cidr in networks):
            raise ValueError("invalid tenant network allowlist")
        allowed = tuple(ipaddress.ip_network(cidr, strict=True) for cidr in networks)
        ledger = AcceptedWorkLedger(args.fee_min_work)
        allocator = FairnessAllocator(ledger, max_active=relay.limit, max_pending=relay.limit)
        profiles[name] = dict(port=port, endpoint=(host, pool_port), backup=backup, worker=worker, allowed=allowed,
                              windows=EventFeeWindows(allocator, args.max_fee_window_seconds,
                                                      args.fee_cooldown_seconds, args.fee_overdraft, relay.fee_capacity))
        ports.add(port)
    return profiles


async def run(args, stop_event=None):
    try:
        import resource
    except ImportError:
        pass  # Windows does not expose RLIMIT_NOFILE via resource.
    else:
        soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
        warning = fd_limit_warning(args.max_connections,soft,2 if args.active_router else 1)
        if warning:
            LOG.warning("%s; test capacity before use", warning)
    backup = (args.backup_host, args.backup_port) if args.backup_host else None
    url_tls_context = (ssl.create_default_context(cafile=args.tls_ca_file)
                       if getattr(args, 'tls_ca_file', None) else None)
    relay = Relay(args.pool_host, args.pool_port, tls=args.tls, backup=backup,
                  limit=args.max_connections, timeout=args.connect_timeout, allowed_networks=args.allow_cidr,
                  idle_timeout=args.idle_timeout, miner_timeout=getattr(args, "miner_timeout", 600),
                  handshake_timeout=getattr(args, "handshake_timeout", 30),
                  max_miner_message=getattr(args, "max_miner_message", 4096),
                  tls_context=url_tls_context if args.tls else None)
    relay.url_tls_context = url_tls_context
    relay.active_router = args.active_router
    relay.aggregate_worker_mode = getattr(args, "aggregate_worker_mode", "exact")
    relay.aggregate_shard_size = getattr(args, "aggregate_shard_size", 256)
    relay.aggregate_vardiff = getattr(args, "aggregate_vardiff", False)
    relay.tp_mode = getattr(args, "tp_mode", False)
    relay.compatibility = CompatibilityRegistry(getattr(args, 'compatibility_state', 'state/compatibility.json'))
    relay.compatibility.load()
    relay.hot_switch_mode = getattr(args, 'hot_switch_mode', 'auto')
    relay.asic_extranonce2_min = getattr(args, 'asic_extranonce2_min', 0)
    relay.asic_extranonce2_max = getattr(args, 'asic_extranonce2_max', 4)
    relay.fee_jitter_seconds = getattr(args, 'devfee_jitter_seconds', 180)
    relay.fee_connection_gate = FeeConnectionGate(getattr(args, 'devfee_connect_interval', .25))
    relay.job_ttl = getattr(args, 'job_ttl', 900)
    relay.job_gc_interval = getattr(args, 'job_gc_interval', 60)
    relay.trace_stratum = getattr(args, "debug_stratum", False)
    relay.metrics._log_handler = next((handler for handler in LOG.handlers if isinstance(handler, SafeQueueHandler)), None)
    relay.validate_pow = args.validate_pow
    relay.failover_health = (
        getattr(args, 'failover_job_timeout', 120),
        getattr(args, 'failover_max_rtt', 2),
        getattr(args, 'failover_recovery', 30),
    )
    relay.fee_test_worker = getattr(args, "fee_test_worker", None)
    relay.fee_test_port = args.listen_port
    relay.fee_test_percent = 1
    relay.fee_locked = getattr(args, "fee_locked", False)
    relay.fee = (args.devfee_host, args.devfee_port) if args.devfee_host else None
    relay.fee_worker = args.devfee_worker
    relay.fee_worker_template = getattr(args,'devfee_worker_template',None)
    relay.fee_password = args.devfee_password
    relay.user_password = os.environ.get("CHEFAS_PROXY_USER_PASSWORD", "x")
    relay.reconnect_min_seconds = args.reconnect_min_seconds
    relay.require_devfee = args.require_devfee
    relay.fee_grace_seconds = args.fee_grace_seconds
    relay.fee_blocked = False
    relay.ledger = AcceptedWorkLedger(args.fee_min_work)
    relay.fee_allocator = FairnessAllocator(relay.ledger, max_active=relay.limit, max_pending=relay.limit)
    relay.fee_capacity = FeeCapacity()
    relay.fee_windows = EventFeeWindows(relay.fee_allocator, getattr(args, "max_fee_window_seconds", 60),
                                        getattr(args, "fee_cooldown_seconds", 5), getattr(args, "fee_overdraft", .02),
                                        relay.fee_capacity)
    history = HistoryStore(relay.workers, args.history_file) if args.history_file else None
    if history:
        history.load()
    stream = DashboardStream(relay.live_snapshot)
    control = DashboardControl(relay, stream, os.environ.get("CHEFAS_PROXY_ADMIN_TOKEN"), args.control_state)
    if getattr(args, 'aggregate', False):
        from aggregation import ExtranonceSizeCache, VirtualPrefixAllocator
        from router import open_aggregate_channel
        from upstream_manager import SharedUpstreamManager

        if relay.fee and (relay.require_devfee or relay.fee_worker_template):
            raise ValueError('aggregation requires optional DevFee and a fixed fee worker')
        relay.aggregate_sizes = ExtranonceSizeCache()
        local_size = min(relay.asic_extranonce2_max, max(4, relay.asic_extranonce2_min))
        # The private switching listener uses a four-byte extranonce1, even
        # with a single connection. A one-byte value was observed on the
        # hardware session that stayed authorized but stopped submitting.
        prefix_bytes = 4 if relay.fee_test_worker else (1 if relay.limit <= 256 else 2)
        relay.aggregate_prefixes = VirtualPrefixAllocator(prefix_bytes, local_size, maximum_workers=relay.limit)
        relay.shared_upstreams = SharedUpstreamManager(lambda key: open_aggregate_channel(relay, key),
                                                       max_groups=max(128, relay.limit * 3))
    relay.route_configs = load_tenant_profiles(getattr(args, "tenant_profiles", None), args.listen_port, relay, args)
    if relay.route_configs:
        main_ledger = AcceptedWorkLedger(args.fee_min_work)
        main_allocator = FairnessAllocator(main_ledger, max_active=relay.limit, max_pending=relay.limit)
        relay.main_fee_windows = EventFeeWindows(main_allocator, args.max_fee_window_seconds,
                                                args.fee_cooldown_seconds, args.fee_overdraft, relay.fee_capacity)
    journal = None
    if args.active_router:
        relay.metrics._ledger = relay.ledger
        relay.metrics._devfee_enabled = bool(relay.fee)
        relay.metrics._audit = WorkAudit(relay.ledger)
        journal_path = getattr(args, 'devfee_journal', None)
        if journal_path is None:
            journal_path = Path(args.control_state).with_name('devfee.sqlite3')
        if journal_path:
            journal = DevFeeJournal(journal_path)
            journal.restore_budget(relay.ledger)
            if relay.route_configs:
                journal.restore_budget(relay.main_fee_windows.allocator.ledger, "main")
                for tenant, profile in relay.route_configs.items():
                    journal.restore_budget(profile["windows"].allocator.ledger, tenant)
            relay.metrics._devfee_journal = journal
    server = None
    tenant_servers = []
    try:
        server = await asyncio.start_server(relay.handle, args.listen_host, args.listen_port, backlog=512,
                                          limit=relay.max_miner_message if args.active_router else 65536)
        for name, profile in relay.route_configs.items():
            async def handle_tenant(reader, writer, tenant=name):
                await relay.handle(reader, writer, tenant)
            listener = await asyncio.start_server(handle_tenant, args.listen_host, profile["port"],
                                                  backlog=512, limit=relay.max_miner_message)
            tenant_servers.append(listener)
        relay.set_listeners(server, *tenant_servers)
        dashboard = await asyncio.start_server(lambda r, w: dashboard_request(
            r, w, relay.metrics, relay.session_snapshot, relay.status_snapshot, stream, control, relay.prometheus_snapshot),
                                               "127.0.0.1", args.dashboard_port)
    except BaseException:
        for listener in tenant_servers:
            listener.close()
            await listener.wait_closed()
        if server is not None:
            server.close()
            await server.wait_closed()
        if journal is not None:
            await journal.close()
        raise
    LOG.info("Experimental relay; pilot validation required. Listening on %s", server.sockets[0].getsockname())
    LOG.info("Dashboard: http://127.0.0.1:%s", args.dashboard_port)
    if history:
        history.start()
    async def summaries():
        while True:
            await asyncio.sleep(60)
            audit = relay.metrics.snapshot().get("audit", {})
            estimate = audit.get("hashrate_windows", {}).get("5m", {}).get("hashrate_hs")
            event(logging.INFO, "minute_summary", active=relay.active,
                  accepted=relay.metrics.accepted, rejected=relay.metrics.rejected,
                  hashrate_5m_hs=estimate,
                  reconnect_fallbacks=relay.metrics.compatibility_fallbacks,
                  coalesced_read_batches=relay.metrics.coalesced_read_batches,
                  coalesced_extra_messages=relay.metrics.coalesced_extra_messages,
                  logging_dropped=getattr(relay.metrics._log_handler, "dropped", 0))
    async def pilot_summaries():
        handler = RotatingFileHandler(Path(args.log_dir) / "pilot-summary.log", maxBytes=1024 * 1024,
                                      backupCount=3, encoding="utf-8")
        try:
            backup_ok = await pilot_preflight(relay, args.listen_host, args.listen_port)
            while True:
                metrics = relay.metrics
                saving_value = metrics.wan_comparison()["estimated_saving_percent"]
                saving = f"{saving_value:.1f}% est." if saving_value is not None else "n/a"
                groups = relay.shared_upstreams.groups if relay.shared_upstreams else ()
                sockets = sum(not group.connection.closed for group in groups)
                active = sum(session["worker"] is not None for session in relay.sessions.values())
                failover = ("Not configured" if relay.backup is None else
                            "Ready (probe)" if backup_ok else "Unreachable at startup")
                line = (f"[PILOT] ASICs: {active} | Sockets: {sockets} | Traffic saved: {saving} | "
                        f"Shares OK/Rej: {metrics.accepted}/{metrics.rejected} | Failover: {failover}")
                print(line, flush=True)
                handler.emit(logging.LogRecord("pilot", logging.INFO, "", 0, line, (), None))
                await asyncio.sleep(60)
        finally:
            handler.close()

    pilot_task = asyncio.create_task(pilot_summaries()) if getattr(args, "pilot_console", False) else None
    summary = asyncio.create_task(summaries())
    fleet = asyncio.create_task(relay.reconcile_idle_fee_sessions())
    try:
        if stop_event is None:
            await server.serve_forever()
        else:
            await stop_event.wait()
    finally:
        # Python 3.12 Server.wait_closed() also waits for accepted transports.
        # Stop admission first, then close miners and SSE clients before waiting
        # for the listeners. An async-with exit here deadlocks on live clients.
        for listener in (server, dashboard, *tenant_servers):
            listener.close()
        summary.cancel()
        fleet.cancel()
        if pilot_task is not None:
            pilot_task.cancel()
        await asyncio.gather(summary, fleet, *((pilot_task,) if pilot_task else ()), return_exceptions=True)
        await asyncio.gather(*(control._reconnect(session) for session in list(relay.sessions.values())))
        await stream.close()
        await control.observer_stream.close()
        await control.public_stream.close()
        await relay.shutdown()
        for listener in (server, dashboard, *tenant_servers):
            await listener.wait_closed()
        if journal is not None:
            await journal.close()
        if history:
            await history.close()
        event(logging.INFO, "proxy_stopped", accepted=relay.metrics.accepted, rejected=relay.metrics.rejected)


def run_event_loop(args):
    if sys.platform != "win32" or not args.aggregate:
        asyncio.run(run_cli(args), debug=False)
        return

    from asyncio.proactor_events import _ProactorSocketTransport

    class CompactTransport(_ProactorSocketTransport):
        def __init__(self, *parameters):
            super().__init__(*parameters)
            # CPython schedules the first overlapped read with call_soon.
            # Shrink only the known receive buffer before that callback runs.
            if isinstance(getattr(self, "_data", None), bytearray):
                self._data = bytearray(8192)

    class CompactLoop(asyncio.ProactorEventLoop):
        def _make_socket_transport(self, sock, protocol, waiter=None, extra=None, server=None):
            if server is None:
                return super()._make_socket_transport(sock, protocol, waiter, extra, server)
            return CompactTransport(self, sock, protocol, waiter, extra, server)

    class CompactPolicy(asyncio.WindowsProactorEventLoopPolicy):
        def new_event_loop(self):
            return CompactLoop()

    original = asyncio.get_event_loop_policy()
    try:
        asyncio.set_event_loop_policy(CompactPolicy())
        asyncio.run(run_cli(args), debug=False)
    finally:
        asyncio.set_event_loop_policy(original)


async def run_cli(args):
    signals = ShutdownSignals()
    try:
        signals.install()
        await run(args, signals.stop)
    finally:
        signals.close()





def main():
    parser = argparse.ArgumentParser(prog="chefas-proxy", description=__doc__, allow_abbrev=False)
    parser.add_argument("--pool-host", required=True)
    parser.add_argument("--env-file", default=".env", help="Explicit dotenv path; server ENV takes priority; empty disables")
    parser.add_argument("--log-dir", default="logs")
    parser.add_argument("--log-level", choices=("INFO", "WARNING", "DEBUG"), default="INFO")
    parser.add_argument("--debug-stratum", action="store_true", help="Allow redacted notify/submit packet traces at DEBUG")
    parser.add_argument("--miner-timeout", type=float, default=600, help="ASIC silence: valid submit/ping in active mode, inbound bytes otherwise; 0 disables")
    parser.add_argument("--handshake-timeout", type=float, default=30, help="Active-mode authorization deadline")
    parser.add_argument("--max-miner-message", type=int, default=4096, help="Maximum active-mode ASIC JSON object size, 512..65536")
    parser.add_argument("--control-state", default="state/control.json")
    parser.add_argument("--history-file", default="state/history.json", help="Worker history checkpoint path; empty disables persistence")
    parser.add_argument("--devfee-journal",
                        help="SQLite work journal path (default devfee.sqlite3 next to control state; empty disables)")
    parser.add_argument("--pool-port", type=int, choices=range(1, 65536), metavar="PORT")
    parser.add_argument("--listen-host", help="Stratum bind address (default 0.0.0.0 with private IPv4 access)")
    parser.add_argument("--listen-port", type=int, default=3333, choices=range(1, 65536), metavar="PORT")
    parser.add_argument("--tls", action="store_true", help="Verify TLS certificate on upstream connection")
    parser.add_argument('--tls-ca-file', help='Optional trusted CA PEM for TLS URLs and --tls; verification remains mandatory')
    parser.add_argument('--hot-switch-mode', choices=('auto','reconnect'), default='auto')
    parser.add_argument('--compatibility-state', default='state/compatibility.json')
    parser.add_argument('--asic-extranonce2-min', type=int, default=0)
    parser.add_argument('--asic-extranonce2-max', type=int, default=4)
    parser.add_argument("--active-router", "--stateful", action="store_true", help="Enable experimental stateful Stratum router")
    parser.add_argument("--aggregate", action="store_true",
                        help="Share compatible pool connections (requires active routing)")
    parser.add_argument("--tp-mode", action="store_true", help="Raw TCP relay without Stratum observation; applies at session admission")
    parser.add_argument("--validate-pow", action="store_true")
    parser.add_argument("--devfee-host")
    parser.add_argument('--devfee-jitter-seconds', type=float, default=180)
    parser.add_argument('--devfee-connect-interval', type=float, default=.25)
    parser.add_argument('--job-ttl', type=float, default=900)
    parser.add_argument('--job-gc-interval', type=float, default=60)
    parser.add_argument("--devfee-port", type=int, choices=range(1, 65536), metavar="PORT")
    parser.add_argument("--devfee-worker")
    parser.add_argument('--devfee-worker-template',help='Explicit fee session suffix, e.g. {worker}_{slot}; default preserves the account exactly')
    parser.add_argument("--fee-min-work", type=float, default=0)
    parser.add_argument("--max-fee-window-seconds", type=float, default=60)
    parser.add_argument("--fee-cooldown-seconds", type=float, default=5)
    parser.add_argument("--fee-overdraft", type=float, default=.02, help="Fraction of one fee share allowed beyond quota")
    parser.add_argument("--require-devfee", action="store_true", help="Stop all active sessions after sustained DevFee failure; restart required")
    parser.add_argument("--fee-grace-seconds", type=float, default=180, help="DevFee outage/rejection grace in required mode")
    parser.add_argument("--reconnect-min-seconds", type=float, default=5940)
    parser.add_argument("--backup-host", help="Backup pool, used when initial connection fails")
    parser.add_argument("--backup-port", type=int, choices=range(1, 65536), metavar="PORT")
    parser.add_argument("--failover-job-timeout", type=float, default=120, metavar="SECONDS",
                        help="Maximum age of a shared pool job before failover")
    parser.add_argument("--failover-max-rtt", type=float, default=2, metavar="SECONDS",
                        help="Shared pool submit latency threshold")
    parser.add_argument("--failover-recovery", type=float, default=30, metavar="SECONDS",
                        help="Fresh-job stability period before returning to a degraded pool")
    parser.add_argument("--dashboard-port", type=int, default=8080, choices=range(1, 65536), metavar="PORT")
    parser.add_argument("--max-connections", type=int, default=512, metavar="N")
    parser.add_argument("--aggregate-worker-mode", choices=("exact", "account"), default="exact")
    parser.add_argument("--aggregate-shard-size", type=int, choices=range(1, 1025), default=256, metavar="N")
    parser.add_argument("--aggregate-vardiff", action="store_true")
    parser.add_argument("--tenant-profiles", help="Path to JSON port profiles")
    parser.add_argument("--connect-timeout", type=float, default=10, metavar="SECONDS")
    parser.add_argument("--idle-timeout", type=float, default=0, metavar="SECONDS",
                        help="Close sessions with no traffic in either direction (0 disables)")
    parser.add_argument("--allow-cidr", action="append", default=[], metavar="NETWORK",
                        help="Allow miner source network (repeatable, e.g. 192.168.10.0/24)")
    parser.add_argument("--allow-any-miner", action="store_true",
                        help="Explicitly allow all miner source addresses on a non-loopback listener")
    parser.add_argument("--fee-test-worker", metavar="WORKER",
                        help="Enable rate switching for an exact worker on a private listener")
    parser.add_argument("--pilot-console", action="store_true")
    args = parser.parse_args()
    args.fee_locked = release_fee_locked()
    if args.fee_locked:
        protected = ("--devfee", "--fee-", "--require-devfee", "--max-fee-window", "--tp-mode", "--transparent")
        if any(arg.startswith(protected) for arg in sys.argv[1:]):
            parser.error("Fee policy and transparent mode are locked in the operator release")
        args.active_router = args.aggregate = args.validate_pow = True
        args.devfee_host, args.devfee_port = "stratum+ssl://btc-ssl.viabtc.io", 551
        args.devfee_worker = "chefasfee.001"
        args.devfee_jitter_seconds = 15
    if not 0 <= args.asic_extranonce2_min <= args.asic_extranonce2_max <= 32:
        parser.error('ASIC extranonce2 bounds must satisfy 0 <= min <= max <= 32')
    for role in ('pool', 'backup', 'devfee'):
        host, port = getattr(args, role+'_host'), getattr(args, role+'_port')
        if host:
            try:
                _, port, _ = parse_endpoint(host, port)
            except ValueError:
                parser.error('invalid '+role+' endpoint; supply a port or matching Stratum URL')
            setattr(args, role+'_port', port)
        elif port is not None:
            parser.error(role+' port requires a host')
    for name, lower, upper in (('devfee_jitter_seconds',0,3600), ('devfee_connect_interval',0,60),
                               ('job_ttl',.1,86400), ('job_gc_interval',.1,3600)):
        value = getattr(args,name)
        if not math.isfinite(value) or not lower <= value <= upper:
            parser.error('--'+name.replace('_','-')+' outside allowed range')
    load_environment(args.env_file)
    tp_env = os.environ.get("TP_MODE", "false").lower()
    if tp_env not in ("true", "false"):
        parser.error("TP_MODE must be true or false")
    args.tp_mode = not args.fee_locked and (args.tp_mode or tp_env == "true")
    if args.aggregate and (not args.active_router or args.tp_mode):
        parser.error('--aggregate requires --active-router without TP mode')
    if args.fee_test_worker is not None and (
        not args.aggregate or not args.devfee_host
        or args.tenant_profiles or args.backup_host or not os.environ.get("CHEFAS_PROXY_ADMIN_TOKEN")
        or not 1 <= len(args.fee_test_worker) <= 128 or not args.fee_test_worker.isprintable()
    ):
        parser.error("--fee-test-worker requires aggregation, a fee pool, an admin token, no backup pool and no tenant profiles")
    if args.aggregate and (args.require_devfee or args.devfee_worker_template):
        parser.error('--aggregate requires optional DevFee and a fixed fee worker')
    if args.tp_mode:
        args.require_devfee = False
    args.devfee_password = "d=32768" if args.fee_locked else os.environ.get("CHEFAS_PROXY_DEVFEE_PASSWORD", "x")
    if not math.isfinite(args.miner_timeout) or (args.miner_timeout != 0 and not .1 <= args.miner_timeout <= 86400):
        parser.error("--miner-timeout must be 0 or .1..86400")
    if not math.isfinite(args.handshake_timeout) or not .1 <= args.handshake_timeout <= 300:
        parser.error("--handshake-timeout must be .1..300")
    if not 512 <= args.max_miner_message <= 65536:
        parser.error("--max-miner-message must be 512..65536")
    if args.require_devfee and not (args.active_router and args.devfee_host and args.devfee_port and args.devfee_worker):
        parser.error("--require-devfee requires active routing and complete DevFee configuration")
    if not math.isfinite(args.fee_grace_seconds) or not 0.1 <= args.fee_grace_seconds <= 86400:
        parser.error("--fee-grace-seconds must be in 0.1..86400")
    if any((args.devfee_host, args.devfee_port, args.devfee_worker)) and not all((args.devfee_host, args.devfee_port, args.devfee_worker)):
        parser.error("--devfee-host, --devfee-port and --devfee-worker must be supplied together")
    if (args.devfee_host or args.validate_pow) and not args.active_router and not args.tp_mode:
        parser.error("DevFee and PoW validation require --active-router")
    if args.active_router and args.idle_timeout:
        parser.error("--idle-timeout is not yet supported with --active-router")
    if not 0.25 <= args.max_fee_window_seconds <= 3600 or not 0 <= args.fee_cooldown_seconds <= 3600 or not 0 <= args.fee_overdraft <= .1:
        parser.error("invalid fee window, cooldown or overdraft")
    if not math.isfinite(args.fee_min_work) or args.fee_min_work < 0 or not math.isfinite(args.reconnect_min_seconds) or args.reconnect_min_seconds < 0:
        parser.error("fee work and reconnect interval must be finite and non-negative")
    if bool(args.backup_host) != bool(args.backup_port):
        parser.error("--backup-host and --backup-port must be supplied together")
    for option, minimum, maximum in (('failover_job_timeout', 1, 3600),
                                      ('failover_max_rtt', .1, 120),
                                      ('failover_recovery', 0, 3600)):
        if not minimum <= getattr(args, option) <= maximum:
            parser.error('--' + option.replace('_', '-') + ' outside allowed range')
    if not 1 <= args.max_connections <= 10000:
        parser.error("--max-connections must be in 1..10000")
    if args.devfee_worker_template is not None:
        try:
            stable_fee_worker(args.devfee_worker,args.max_connections,args.devfee_worker_template)
        except ValueError:
            parser.error('invalid --devfee-worker-template or expanded worker name')
    if not math.isfinite(args.connect_timeout) or not 0.1 <= args.connect_timeout <= 120:
        parser.error("--connect-timeout must be in 0.1..120 seconds")
    if not math.isfinite(args.idle_timeout) or (args.idle_timeout != 0 and not 0.1 <= args.idle_timeout <= 86400):
        parser.error("--idle-timeout must be 0 or in 0.1..86400 seconds")
    if args.listen_host is None:
        args.listen_host = "0.0.0.0"
        if not args.allow_cidr and not args.allow_any_miner:
            args.allow_cidr = DEFAULT_LAN_CIDRS
    try:
        args.allow_cidr = [ipaddress.ip_network(value, strict=True) for value in args.allow_cidr]
    except ValueError as exc:
        parser.error(f"invalid --allow-cidr: {exc}")
    if args.allow_any_miner and args.allow_cidr:
        parser.error("--allow-any-miner cannot be combined with --allow-cidr")
    if args.fee_test_worker is not None and not is_loopback_bind(args.listen_host):
        if len(args.allow_cidr) != 1 or args.allow_cidr[0].num_addresses != 1:
            parser.error("--fee-test-worker on a non-loopback listener requires exactly one allowed source IP")
    if not is_loopback_bind(args.listen_host) and not args.allow_cidr and not args.allow_any_miner:
        parser.error("non-loopback listeners require --allow-cidr or explicit --allow-any-miner")
    pilot = None
    try:
        pilot = PilotLogging(args.log_dir, args.log_level)
        run_event_loop(args)
    except KeyboardInterrupt:
        pass
    except (SecretConfigurationError, ValueError, OSError):
        LOG.error("Startup or configuration failed; check configuration, ENV and secret key")
        raise SystemExit(1) from None
    finally:
        if pilot and not pilot.close():
            LOG.warning("Pilot log drain incomplete")


if __name__ == "__main__":
    main()
