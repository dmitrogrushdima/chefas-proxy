# Operator quick start

Use a trusted standalone build made for the pilot machine: it bundles Python and the native core. Start with [Install on your farm](../README.md#download) for the download, installation and ASIC setup. Only the separate native development runtime needs an installed matching Python; see [development](development.md).

## Configure the server

The standalone launchers generate `chefas-proxy.json` and private `.env`
credentials on first run. On Ubuntu, `install.sh` generates the service's
`/etc/chefas-proxy/chefas-proxy.env` with mode 0600. Existing files are not
replaced. Set your BTC pool in the JSON; keep passwords and tokens out of it.

Open the environment file locally and copy `CHEFAS_PROXY_ADMIN_TOKEN` to sign
in. Keep `CHEFAS_PROXY_SECRETS_KEY` in a private backup: losing it prevents
recovery of encrypted route passwords. Never send either value to support.
`.env.example` is a reference for manual developer setup, not a file to copy
over generated credentials.

Run `start.cmd` on Windows. On Ubuntu, use `sudo systemctl enable --now
chefas-proxy` after configuring the installed JSON. For a manual Linux launch,
use `./start.sh` from the extracted package. Both packages include Python.

The operator release fixes the fee rate and account. Do not copy private VPS
flags such as `--fee-test-worker` or development DevFee overrides into your
farm configuration. Configure only your own customer pool and ASIC identities.

## Pool and miner addresses

The default miner listener is `0.0.0.0:3333`, restricted to loopback and private IPv4 sources. Use `--allow-cidr YOUR_LAN_CIDR` and restrict the port in the host firewall to your farm subnet. For local-only use, set `--listen-host 127.0.0.1`.

`--pool-host` and `--devfee-host` accept bare hosts with separate port flags or full `stratum+ssl://HOST:PORT` URLs. SSL URLs verify the certificate and hostname. `--tls-ca-file PATH` supplies a private CA; it does not disable verification. `--tls` forces TLS for every upstream; omit that global flag when intentionally mixing TCP and SSL routes.

Configure each pilot ASIC with:

| ASIC field | Value |
| --- | --- |
| Pool URL | `stratum+tcp://PROXY_LAN_IP:3333` |
| Worker | Its normal **user-pool** account/worker name |
| Password | Its normal user-pool password |

The ASIC-facing connection is TCP, even when upstreams use TLS. Preserve a direct-pool backup entry on the ASIC according to the farm operator's availability policy. The proxy's `--backup-host/--backup-port` is a separate upstream failover mechanism.

Enable `--active-router --validate-pow` for stateful routing and local PoW checks. Configure all three fee fields (`--devfee-host`, port or URL port, `--devfee-worker`) when using DevFee. `--require-devfee` explicitly stops mining after a sustained fee outage; restarting a still-broken configuration does not fix that outage.

## Open the dashboard

**On the proxy computer**, open `http://127.0.0.1:8080`. Select **Sign in**
and enter the value of `CHEFAS_PROXY_ADMIN_TOKEN` from your configuration.
This token belongs to the farm operator.

**From another computer**, open PowerShell or a terminal and run the command
below. Replace `operator` with your SSH username and `192.168.1.50` with
the farm server's LAN address. The farm server needs an SSH service and
must be reachable from your computer, for example over the farm LAN or VPN.

```sh
ssh -N -o ExitOnForwardFailure=yes -L 127.0.0.1:18080:127.0.0.1:8080 operator@192.168.1.50
```

Keep the terminal open and visit `http://127.0.0.1:18080` on your computer.
If you changed the dashboard port, replace the final `8080` in the command.

**Give someone read-only access** from **Routes в†’ Create Observer Link**.
The built-in link shows the proxy's worker statistics and expires after
one hour. Share it privately with someone allowed to see that farm's
statistics. The recipient must be able to reach the URL in the link.
If it starts with `127.0.0.1:18080`, they need their own tunnel on that
local port. For an HTTPS dashboard already configured by your farm
administrator, generate the link from that HTTPS address.

| What you see | What to check |
| --- | --- |
| Dashboard fails to open | Check that CHEFAS is running. On Ubuntu use `sudo systemctl status chefas-proxy` and `sudo journalctl -u chefas-proxy -n 30 --no-pager`. From a laptop, also check the SSH tunnel and its local port. |
| Dashboard opens but no workers appear | Verify the ASIC's Pool 1 uses the farm server's LAN IP and Stratum port, usually `3333`. Check that the ASIC selected Pool 1, its subnet is allowed, and the pool accepts its worker credentials. |
| Worker is online but share count is zero | Check whether the miner has submitted work and whether the upstream pool accepts it. Hashrate estimates need accepted work and time to fill their measurement window. |
| Observer link fails | Check that the recipient can reach the dashboard and that the one-hour link is still valid. Create a fresh link after expiry. |

## Verify before expanding

Check accepted shares at **both pools**, compare pool rejects, inspect the audit export and confirm worker identity. Measure reconnects and hashrate over complete windows; an estimate during warmup is not farm capacity. Test primary failure, restoration and an orderly shutdown on the actual OS. Record ASIC model, firmware version and pool endpoint in the [compatibility matrix](compatibility.md).

If chips disconnect after hot changes, use `--hot-switch-mode reconnect` while investigating. Automatic fallback profiles are local, expire after 24 hours and can be reset by stopping the process and removing the chosen compatibility-state file. Start with conservative defaults; do not copy zero-jitter settings from laboratory tests.

State lives in `state/`, rotated logs in `logs/`. Restrict both directories and `.env` to the service account. SIGINT/SIGTERM perform orderly cleanup; a forced kill can lose the last checkpoint. The UI shows only measured data: ASIC network RTT is not invented from mining time.

Defaults currently allow 512 TCP connections and retain at most 512 worker-history identities. Raising `--max-connections` alone does not enlarge the history table. A multi-thousand-device rollout requires capacity and telemetry scaling work; do not infer support from a higher numeric connection limit.

## Rollback and upgrades

For immediate rollback, restore the ASIC's original direct-pool entry as Pool 1
and confirm accepted work at the pool before stopping CHEFAS. This is also the
safe path while changing proxy binaries; an upgrade can require reconnects.

On Windows, keep the old package folder. Stop its proxy, back up
`chefas-proxy.json`, `.env` and `state/` privately, then copy these operator
files into a freshly extracted new package. Keep the new executable with its
own new `_internal`; do not mix libraries from different versions. Run
`start.cmd`, verify one ASIC and restore the old package if checks fail.

The Linux installer performs first installation and refuses to overwrite an
existing `/opt/chefas-proxy` or service unit. Do not delete the installation to
bypass that check: keep `/etc/chefas-proxy`, `/var/lib/chefas-proxy` and the
previous package backed up before a planned maintenance upgrade. For the first
public pilot, prefer the direct-pool fallback during maintainer-assisted Linux
upgrades rather than replacing files beneath a running process.
