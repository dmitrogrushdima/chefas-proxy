# CHEFAS Proxy naming and upgrade notes

Public name: **CHEFAS Proxy**. Executable, package, systemd unit and directory slug: `chefas-proxy`. Scope: Bitcoin SHA-256d over Stratum V1 only. Canonical repository: `https://github.com/dmitrogrushdima/chefas-proxy`. Update existing Git remotes to this URL after the repository rename.

## Upgrading a previous pilot

This is an explicit naming migration. Stop the old service gracefully, preserve private state and its encryption key, then install the new build in a separate directory. Never mix native modules from different builds.

- Rename the previous public configuration to `chefas-proxy.json` and review its paths. The supplied template is `chefas-proxy.example.json`.
- Environment names now use `CHEFAS_PROXY_`: `ADMIN_TOKEN`, `SECRETS_KEY`, `USER_PASSWORD`, `DEVFEE_PASSWORD`, `PYTHON`, `BUILD_PYTHON`, `TEST_ENTRYPOINT` and `TEST_EXECUTABLE`.
- Keep the **same existing encryption key value** under the new name when retaining encrypted route state. For persisted ENV references in `control.json`, update only the old environment-variable name to its new equivalent. Do not replace ciphertext or reveal decrypted passwords.
- Linux defaults are `/opt/chefas-proxy`, `/etc/chefas-proxy/chefas-proxy.json`, `/etc/chefas-proxy/chefas-proxy.env`, `/var/lib/chefas-proxy` and `/var/log/chefas-proxy`; service user and unit are `chefas-proxy`. Copy private state deliberately with correct ownership. Disable the old unit before starting the new one to avoid port conflicts.
- Browser storage uses a new namespace. Sign in again and reselect the language if needed; old browser tokens are not imported automatically.
- No automatic migration of a running installation, repository rename, license selection or release publication is performed by the build scripts.

Use `python launch.py` for source development. Root launchers prefer a matching standalone build if present, so rebuilding is required to test source changes through those launchers.

## Public claims

Version `0.1.0rc1` identifies the existing build line, not a production certification. Physical ASIC models and pool combinations remain pilot targets. The shared TCP aggregation path is unfinished. Official Telegram: [news](https://t.me/Chefasproxy), [community](https://t.me/Chefasproxychat). A project license has not been chosen; do not display a license badge claiming permissions that have not been granted.
