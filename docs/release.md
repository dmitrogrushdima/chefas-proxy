# Standalone pilot releases

Release builds contain an executable, a bundled Python runtime, compiled core
modules, and the CHEFAS Proxy dashboard. Operators do not need Python or Cython.
Keep the executable and its `_internal` directory together.

## Operator: three steps

1. Unpack the **matching Windows x64 or Linux x64** release. On Windows,
   `start.cmd` creates `chefas-proxy.json` on first launch and opens it for editing.
   On Linux, copy `chefas-proxy.example.json` to `chefas-proxy.json` manually.
   Replace `POOL_HOST` and set the pool address/port. Aggregation is enabled by
   the release template. Set both `backup_host` and `backup_port` for Smart Failover. The default Stratum listener binds to `0.0.0.0` and
   accepts loopback and private IPv4 addresses. Use an explicit `allow_cidr`
   list to restrict access to the farm subnet (for example
   `["192.168.10.0/24"]`). ASIC wallet/worker credentials normally come from
   each ASIC's Stratum authorization. Route overrides are configured in CHEFAS Proxy.
2. Copy `.env.example` to `.env`, generate your own admin token and encryption
   key, and set `CHEFAS_PROXY_ADMIN_TOKEN` / `CHEFAS_PROXY_SECRETS_KEY`. Restrict the file to
   the service owner. Never place tokens or passwords in `chefas-proxy.json`. DevFee
   targets 1% of accepted difficulty and is built into the operator release.
   The operator JSON configures the customer routes and listener. The release
   attempts verified TLS to ViaBTC and can fall back to TCP. Pool credentials
   saved by the routing API use the encrypted secret store.
3. Run `start.cmd` / `chefas-proxy.exe` (Windows) or `./chefas-proxy` (Linux). `start.cmd`
   keeps the console open after startup errors. With `chefas-proxy.json` present
   it starts without CLI arguments. Open `http://127.0.0.1:8080`. The dashboard
   stays loopback-only; use an authenticated SSH tunnel for remote access.

`--config /absolute/path/chefas-proxy.json` selects another configuration. Relative
state and dotenv paths are resolved from that configuration's directory.
Additional CLI options override scalar configuration settings. Repeated
`--allow-cidr` values add to the configured list. Use `--help` for all proxy
flags or `--release-info` to check that core modules loaded as native binaries.
Do not expose Stratum or the dashboard publicly without the deployment controls
described in the security documentation.

## Maintainer build

Build on the target OS using native **x64 Python**, a C compiler and Python
headers. Linux releases must be built on the oldest supported Linux/glibc
baseline and tested on every declared target; this is not cross-compilation.
Use a dedicated virtual environment:

```text
python -m venv .venv-release
<venv-python> -m pip install -r requirements-release.txt
```

Windows:

```powershell
.\build_release.ps1 -Python .\.venv-release\Scripts\python.exe
```

Linux:

```sh
CHEFAS_PROXY_BUILD_PYTHON=.venv-release/bin/python ./build_release.sh
```

The builder compiles all `build_ext.py` modules, supplies Cython's hidden
imports to PyInstaller, bundles dashboard assets, verifies native core files,
and runs `--release-info` / `--help` with developer Python paths removed.
It refuses existing output directories and retains staging on failure.
Run one native/release build at a time per checkout: the C compiler's
intermediate directory is shared by the native and standalone builders.
The default destination is `dist/chefas-proxy-windows-x64` or
`dist/chefas-proxy-linux-x64`, with a `.zip` (Windows) or `.tar.gz` (Linux) archive
and a companion archive SHA-256 checksum. No secrets, history, control state, or compatibility
profiles are collected from the workspace.

For pools that allow worker suffixes, the public setting
`"devfee_worker_template": "{worker}_{slot}"` opts into stable per-session
worker names derived from the configured `devfee_worker`. Omit it to preserve
the exact account string; wallet and worker syntax varies by pool.

`release-manifest.json` records dependency versions and SHA-256 file hashes.
Pinned top-level requirements and manifests make builds inspectable, but they
do **not** guarantee byte-for-byte reproducibility: compiler/Python versions,
transitive build dependencies and OS libraries must also be controlled for that.
The manifest is unsigned; release signing remains a separate distribution step.
Compilation raises the cost of source modification; it cannot make DevFee
removal or binary patching impossible.

## Linux service

Verify the release checksum and extract the Linux x64 archive. From the extracted
release directory:

```sh
sudo bash install.sh
sudoedit /etc/chefas-proxy/chefas-proxy.json /etc/chefas-proxy/chefas-proxy.env
sudo systemctl enable --now chefas-proxy
sudo systemctl status chefas-proxy --no-pager
```

The installer is for fresh deployments. It installs the executable and runtime
under `/opt/chefas-proxy`, preserves existing configuration files and leaves the
service stopped until the operator configures it. The unit runs as the
unprivileged `chefas-proxy` account with `Restart=always` and
`LimitNOFILE=65535`. The root-owned environment file is mode 0600 and is read
by the systemd manager. State and logs live in `/var/lib/chefas-proxy` and
`/var/log/chefas-proxy`. The host needs systemd with support for
`StateDirectory` and `LogsDirectory`. Use service ports above 1023.

### Prometheus and DevFee journal

For the aggregation pilot, set `"aggregate": true` alongside
`"active_router": true` in `chefas-proxy.json`.
Scrape `http://127.0.0.1:8080/metrics` on the proxy host, using the configured
dashboard port if different. LAN and WAN counters measure application bytes
received and queued for transport, excluding TCP and TLS overhead. Share
counters and submit-to-reply latency are labeled by pool endpoint and route.
The active-worker gauge counts authorized sessions. The shared-socket gauge
includes live shared pool connections held ready for routing.

Prometheus counters reset with the process. In active mode, the SQLite journal
at `/var/lib/chefas-proxy/devfee.sqlite3` persists the outcomes the proxy
observed and committed. The dashboard audit export exposes cumulative totals
and recent UTC-day rows under `persistent_work`. It distinguishes accepted,
rejected and unknown outcomes and excludes local-only shares. Alert on
`chefas_proxy_devfee_journal_errors_total` increasing. A share accepted by a
pool just before connection loss or process termination may have no observed
and committed result locally.

Standalone startup checks are packaging checks. They do not certify real ASIC
compatibility, network throughput, Linux deployment, or unattended long-term
operation; these remain pilot qualification tasks.

### Pilot console

The Windows launcher prints local ASIC URLs and primary/backup TCP or TLS
connection timings. It then reports worker, shared-socket and share counts every
minute in the console and in the rotating `logs/pilot-summary.log`. The traffic
percentage compares observed LAN and WAN application bytes in this process.
It is an estimate; use a matched 1:1 baseline to quantify savings.

### DevFee windows in aggregate mode

`max_fee_window_seconds` defaults to 60, `fee_cooldown_seconds` to 5, and
`fee_overdraft` to 0.02 of the expected next fee share. At most one ASIC per
active user group works on the fee route. Groups of up to five ASICs use
a ceiling of 15 seconds or the configured ceiling if shorter. The quota still
requires nearly one full fee share, so a small group with difficult fee work
may wait longer or have fee windows with no accepted share. Above 50 group members, admission also
requires that the candidate account for at most 3% of the group's measured user
work in the last five minutes. A single ASIC in a small group can represent a
larger instantaneous share. A bounded window limits time diverted, while
confirmed pool difficulty determines accounting. Ordinary non-aggregated
routing retains its existing scheduling policy.

The default SQLite journal restores confirmed user and fee work, plus eligible
user work recorded by this version. Older journal rows lack eligibility
metadata, so they are not retroactively made eligible for commission. Use a
separate journal per independent deployment.
