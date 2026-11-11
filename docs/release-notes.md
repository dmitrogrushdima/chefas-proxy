# CHEFAS Proxy — first public pilot

BTC / SHA-256d / Stratum V1. Packages include the native runtime and dashboard;
Python is not required on the farm computer. Extract the full archive and keep
the executable together with `_internal`.

- Windows x64: run `start.cmd`, set your BTC pool in `chefas-proxy.json`, run
  `start.cmd` again. First run generates private credentials in `.env`.
- Ubuntu 24.04 x64: run `sudo bash install.sh`, edit
  `/etc/chefas-proxy/chefas-proxy.json`, then start `chefas-proxy` with systemctl.
  The installer generates a private environment file for the service.
- Open `http://127.0.0.1:8080` on the proxy computer. Sign in using the generated
  `CHEFAS_PROXY_ADMIN_TOKEN`. Keep your existing ASIC worker and pool password.
- The operator release targets a fixed 1% of pool-confirmed difficulty. Its
  dashboard cannot change the rate or expose the maintainer's private controls.

Each release is gated by Windows/Linux source tests, native build and frozen
operator-policy checks. Linux additionally runs a clean installation and
systemd start/restart/stop check. SHA-256 files accompany both archives.

This is a pilot release: start with one ASIC and keep a direct-pool fallback.
Qualification is limited to the recorded tests. Model/firmware combinations,
long-term lossy networks and large physical fleets require their own pilot.
No fixed hashrate, perfectly flat graph, or zero-reject guarantee is made.

See the repository README and `docs/quickstart.md` for installation, remote
access, troubleshooting and rollback. Report reproducible issues without
tokens, passwords or identifying network details.
