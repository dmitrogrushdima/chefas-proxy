# Standalone pilot releases

Release builds contain an executable, a bundled Python runtime, compiled core
modules, and the CHEFAS Proxy dashboard. Operators do not need Python or Cython.
Keep the executable and its `_internal` directory together.

## Operator: three steps

1. Unpack the **matching Windows x64 or Linux x64** release. Copy
   `chefas-proxy.example.json` to `chefas-proxy.json`, replace `POOL_HOST`, and set the
   pool address/port. The default Stratum listener binds to `0.0.0.0` and
   accepts loopback and private IPv4 addresses. Use an explicit `allow_cidr`
   list to restrict access to the farm subnet (for example
   `["192.168.10.0/24"]`). ASIC wallet/worker credentials normally come from
   each ASIC's Stratum authorization. Route overrides are configured in CHEFAS Proxy.
2. Copy `.env.example` to `.env`, generate your own admin token and encryption
   key, and set `CHEFAS_PROXY_ADMIN_TOKEN` / `CHEFAS_PROXY_SECRETS_KEY`. Restrict the file to
   the service owner. Never place tokens or passwords in `chefas-proxy.json`. DevFee
   requires `devfee_host`, `devfee_port`, and `devfee_worker`; its password is
   `CHEFAS_PROXY_DEVFEE_PASSWORD` in ENV. Pool credentials saved by the routing API
   use the existing encrypted secret store.
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

The release contains `chefas-proxy.service.example`; maintainers can generate a
custom unit with `python scripts/generate_service.py --output chefas-proxy.service`.
The generator only writes a new file. It never installs or starts a service.

An administrator must create the `chefas-proxy` service user, install the **complete**
release directory at `/opt/chefas-proxy`, and place configuration at
`/etc/chefas-proxy/chefas-proxy.json` and secrets at `/etc/chefas-proxy/chefas-proxy.env` with restricted
permissions. Install the reviewed unit into `/etc/systemd/system/`, then use
the usual `systemctl daemon-reload` / `systemctl enable --now chefas-proxy` commands.
The generated unit uses systemd-managed `/var/lib/chefas-proxy` and `/var/log/chefas-proxy`,
an unprivileged user, restrictive permissions, and SIGTERM graceful shutdown.
It assumes a systemd version supporting `StateDirectory` / `LogsDirectory`.
Service ports must be above 1023. Review host firewall and process FD limits.

For a **fresh installation**, after unpacking into `/opt/chefas-proxy` and editing
the configuration and environment files locally:

```sh
sudo useradd --system --no-create-home --shell /usr/sbin/nologin chefas-proxy
sudo install -d -m 0750 -o root -g chefas-proxy /etc/chefas-proxy
sudo install -m 0640 -o root -g chefas-proxy chefas-proxy.json /etc/chefas-proxy/chefas-proxy.json
sudo install -m 0600 -o root -g root .env /etc/chefas-proxy/chefas-proxy.env
sudo install -m 0644 /opt/chefas-proxy/chefas-proxy.service.example /etc/systemd/system/chefas-proxy.service
sudo systemctl daemon-reload
sudo systemctl enable --now chefas-proxy
sudo systemctl status chefas-proxy --no-pager
```

The system service manager reads the root-owned environment file. Do not run
these fresh-install steps over an existing deployment without preserving its
private configuration and encryption key. Keep installed binaries owned by root
and readable/executable by the service account. The service creates its own state
and log directories. Linux qualification is still pending; this template is not
evidence of a completed Linux pilot.

Standalone startup checks are packaging checks. They do not certify real ASIC
compatibility, network throughput, Linux deployment, or unattended long-term
operation; these remain pilot qualification tasks.
