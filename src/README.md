# Proxy core

- `proxy.py` starts listeners and applies deployment policy.
- `router.py`, `aggregation.py` and `upstream_manager.py` bind jobs to routes.
- `devfee_scheduler.py` accounts for accepted work and fee reservations.
- `observer.py` and `history_store.py` maintain statistics and committed history.
- `dashboard_server.py` and `dashboard_control.py` provide local access control.

Runtime modules stay flat for matching source and Cython imports. Build tools
are in `scripts/`; generated binaries never belong beside these sources.
See [architecture](../docs/engineering/architecture.md) and
[DevFee accounting](../docs/engineering/devfee.md).
