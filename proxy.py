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
from pathlib import Path
from observer import Metrics, Observer, WorkAudit
from stratum_session import Relay, HALF_CLOSE_GRACE
from dashboard_server import dashboard_request, ASSET_DIR, ASSETS
from devfee_scheduler import AcceptedWorkLedger

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




async def run(args):
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
                  idle_timeout=args.idle_timeout)
    relay.active_router = args.active_router
    relay.validate_pow = args.validate_pow
    relay.fee = (args.devfee_host, args.devfee_port) if args.devfee_host else None
    relay.fee_worker = args.devfee_worker
    relay.fee_password = args.devfee_password
    relay.reconnect_min_seconds = args.reconnect_min_seconds
    relay.require_devfee = args.require_devfee
    relay.fee_grace_seconds = args.fee_grace_seconds
    relay.fee_blocked = False
    relay.ledger = AcceptedWorkLedger(args.fee_min_work)
    if args.active_router:
        relay.metrics._ledger = relay.ledger
        relay.metrics._devfee_enabled = bool(relay.fee)
        relay.metrics._audit = WorkAudit(relay.ledger)
    server = await asyncio.start_server(relay.handle, args.listen_host, args.listen_port, backlog=512)
    relay.set_listeners(server)
    try:
        dashboard = await asyncio.start_server(lambda r, w: dashboard_request(r, w, relay.metrics, relay.session_snapshot, relay.status_snapshot),
                                               "127.0.0.1", args.dashboard_port)
    except BaseException:
        server.close()
        await server.wait_closed()
        raise
    LOG.warning("Experimental relay only; not for production. Listening on %s", server.sockets[0].getsockname())
    LOG.info("Dashboard: http://127.0.0.1:%s", args.dashboard_port)
    try:
        async with server, dashboard:
            await server.serve_forever()
    finally:
        server.close()
        dashboard.close()
        await server.wait_closed()
        await dashboard.wait_closed()
        await relay.shutdown()





def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pool-host", required=True)
    parser.add_argument("--pool-port", type=int, required=True, choices=range(1, 65536), metavar="PORT")
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", type=int, default=3333, choices=range(1, 65536), metavar="PORT")
    parser.add_argument("--tls", action="store_true", help="Verify TLS certificate on upstream connection")
    parser.add_argument("--active-router", "--stateful", action="store_true", help="Enable experimental stateful Stratum router")
    parser.add_argument("--validate-pow", action="store_true")
    parser.add_argument("--devfee-host")
    parser.add_argument("--devfee-port", type=int, choices=range(1, 65536), metavar="PORT")
    parser.add_argument("--devfee-worker")
    parser.add_argument("--devfee-password", default="x")
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
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
