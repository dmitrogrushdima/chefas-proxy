"""Experimental TCP relay; not intended for production mining."""
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
from pathlib import Path
from observer import Metrics, Observer, WorkAudit
from stratum_session import Relay, HALF_CLOSE_GRACE
from dashboard_server import dashboard_request, ASSET_DIR, ASSETS, DashboardStream
from devfee_scheduler import AcceptedWorkLedger
from dashboard_control import DashboardControl
from history_store import HistoryStore
from secret_store import load_environment, SecretConfigurationError
from pilot_logging import PilotLogging, SafeQueueHandler, event
from shutdown_signals import ShutdownSignals

LOG = logging.getLogger("mining_proxy")


def is_loopback_bind(host):
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        # Unknown hostnames are not treated as loopback for security purposes.
        return False


def fd_limit_warning(limit, soft_limit):
    """Conservative estimate, not a guarantee that the OS will accept the load."""
    required = 2 * limit + 64
    if soft_limit >= 0 and soft_limit < required:
        return (f"File descriptor limit {soft_limit} is below the estimated {required} "
                f"needed for {limit} sessions and overhead")
    return None




async def run(args, stop_event=None):
    try:
        import resource
    except ImportError:
        pass  # Windows does not expose RLIMIT_NOFILE via resource.
    else:
        soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
        warning = fd_limit_warning(args.max_connections, soft)
        if warning:
            LOG.warning("%s; test capacity before use", warning)
    backup = (args.backup_host, args.backup_port) if args.backup_host else None
    relay = Relay(args.pool_host, args.pool_port, tls=args.tls, backup=backup,
                  limit=args.max_connections, timeout=args.connect_timeout, allowed_networks=args.allow_cidr,
                  idle_timeout=args.idle_timeout, miner_timeout=getattr(args, "miner_timeout", 600),
                  handshake_timeout=getattr(args, "handshake_timeout", 30),
                  max_miner_message=getattr(args, "max_miner_message", 4096))
    relay.active_router = args.active_router
    relay.trace_stratum = getattr(args, "debug_stratum", False)
    relay.metrics._log_handler = next((handler for handler in LOG.handlers if isinstance(handler, SafeQueueHandler)), None)
    relay.validate_pow = args.validate_pow
    relay.fee = (args.devfee_host, args.devfee_port) if args.devfee_host else None
    relay.fee_worker = args.devfee_worker
    relay.fee_password = args.devfee_password
    relay.user_password = os.environ.get("KONTUR_USER_PASSWORD", "x")
    relay.reconnect_min_seconds = args.reconnect_min_seconds
    relay.require_devfee = args.require_devfee
    relay.fee_grace_seconds = args.fee_grace_seconds
    relay.fee_blocked = False
    relay.ledger = AcceptedWorkLedger(args.fee_min_work)
    history = HistoryStore(relay.workers, args.history_file) if args.history_file else None
    if history:
        history.load()
    stream = DashboardStream(relay.live_snapshot)
    control = DashboardControl(relay, stream, os.environ.get("KONTUR_ADMIN_TOKEN"), args.control_state)
    if args.active_router:
        relay.metrics._ledger = relay.ledger
        relay.metrics._devfee_enabled = bool(relay.fee)
        relay.metrics._audit = WorkAudit(relay.ledger)
    server = await asyncio.start_server(relay.handle, args.listen_host, args.listen_port, backlog=512,
                                      limit=relay.max_miner_message if args.active_router else 65536)
    relay.set_listeners(server)
    try:
        dashboard = await asyncio.start_server(lambda r, w: dashboard_request(r, w, relay.metrics, relay.session_snapshot, relay.status_snapshot, stream, control),
                                               "127.0.0.1", args.dashboard_port)
    except BaseException:
        server.close()
        await server.wait_closed()
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
                  logging_dropped=getattr(relay.metrics._log_handler, "dropped", 0))
    summary = asyncio.create_task(summaries())
    try:
        async with server, dashboard:
            if stop_event is None:
                await server.serve_forever()
            else:
                await stop_event.wait()
    finally:
        summary.cancel()
        await asyncio.gather(summary, return_exceptions=True)
        server.close()
        dashboard.close()
        await server.wait_closed()
        await dashboard.wait_closed()
        if history:
            await history.checkpoint()
        await asyncio.gather(*(control._reconnect(session) for session in list(relay.sessions.values())))
        await stream.close()
        await control.observer_stream.close()
        await relay.shutdown()
        if history:
            await history.close()
        event(logging.INFO, "proxy_stopped", accepted=relay.metrics.accepted, rejected=relay.metrics.rejected)


async def run_cli(args):
    signals = ShutdownSignals()
    try:
        signals.install()
        await run(args, signals.stop)
    finally:
        signals.close()





def main():
    parser = argparse.ArgumentParser(description=__doc__)
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
    parser.add_argument("--pool-port", type=int, required=True, choices=range(1, 65536), metavar="PORT")
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", type=int, default=3333, choices=range(1, 65536), metavar="PORT")
    parser.add_argument("--tls", action="store_true", help="Verify TLS certificate on upstream connection")
    parser.add_argument("--active-router", "--stateful", action="store_true", help="Enable experimental stateful Stratum router")
    parser.add_argument("--validate-pow", action="store_true")
    parser.add_argument("--devfee-host")
    parser.add_argument("--devfee-port", type=int, choices=range(1, 65536), metavar="PORT")
    parser.add_argument("--devfee-worker")
    parser.add_argument("--fee-min-work", type=float, default=0)
    parser.add_argument("--require-devfee", action="store_true", help="Stop all active sessions after sustained DevFee failure; restart required")
    parser.add_argument("--fee-grace-seconds", type=float, default=180, help="DevFee outage/rejection grace in required mode")
    parser.add_argument("--reconnect-min-seconds", type=float, default=5940)
    parser.add_argument("--backup-host", help="Backup pool, used when initial connection fails")
    parser.add_argument("--backup-port", type=int, choices=range(1, 65536), metavar="PORT")
    parser.add_argument("--dashboard-port", type=int, default=8080, choices=range(1, 65536), metavar="PORT")
    parser.add_argument("--max-connections", type=int, default=512, metavar="N")
    parser.add_argument("--connect-timeout", type=float, default=10, metavar="SECONDS")
    parser.add_argument("--idle-timeout", type=float, default=0, metavar="SECONDS",
                        help="Close sessions with no traffic in either direction (0 disables)")
    parser.add_argument("--allow-cidr", action="append", default=[], metavar="NETWORK",
                        help="Allow miner source network (repeatable, e.g. 192.168.10.0/24)")
    parser.add_argument("--allow-any-miner", action="store_true",
                        help="Explicitly allow all miner source addresses on a non-loopback listener")
    args = parser.parse_args()
    load_environment(args.env_file)
    args.devfee_password = os.environ.get("KONTUR_DEVFEE_PASSWORD", "x")
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
    if (args.devfee_host or args.validate_pow) and not args.active_router:
        parser.error("DevFee and PoW validation require --active-router")
    if args.active_router and args.idle_timeout:
        parser.error("--idle-timeout is not yet supported with --active-router")
    if not math.isfinite(args.fee_min_work) or args.fee_min_work < 0 or not math.isfinite(args.reconnect_min_seconds) or args.reconnect_min_seconds < 0:
        parser.error("fee work and reconnect interval must be finite and non-negative")
    if bool(args.backup_host) != bool(args.backup_port):
        parser.error("--backup-host and --backup-port must be supplied together")
    if not 1 <= args.max_connections <= 10000:
        parser.error("--max-connections must be in 1..10000")
    if not math.isfinite(args.connect_timeout) or not 0.1 <= args.connect_timeout <= 120:
        parser.error("--connect-timeout must be in 0.1..120 seconds")
    if not math.isfinite(args.idle_timeout) or (args.idle_timeout != 0 and not 0.1 <= args.idle_timeout <= 86400):
        parser.error("--idle-timeout must be 0 or in 0.1..86400 seconds")
    try:
        args.allow_cidr = [ipaddress.ip_network(value, strict=True) for value in args.allow_cidr]
    except ValueError as exc:
        parser.error(f"invalid --allow-cidr: {exc}")
    if args.allow_any_miner and args.allow_cidr:
        parser.error("--allow-any-miner cannot be combined with --allow-cidr")
    if not is_loopback_bind(args.listen_host) and not args.allow_cidr and not args.allow_any_miner:
        parser.error("non-loopback listeners require --allow-cidr or explicit --allow-any-miner")
    pilot = None
    try:
        pilot = PilotLogging(args.log_dir, args.log_level)
        asyncio.run(run_cli(args))
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
