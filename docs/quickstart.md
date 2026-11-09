# Operator quick start

Use a trusted standalone build made for the pilot machine: it bundles Python and the native core. Start with [Install on your farm](../README.md#install-on-your-farm) for the download, installation and ASIC setup. Only the separate native development runtime needs an installed matching Python; see [development](development.md).

## Configure the server

1. For a standalone launcher, copy `.env.example` to `.env` beside `chefas-proxy.json`. For an Ubuntu service installed with `install.sh`, edit `/etc/chefas-proxy/chefas-proxy.env`. The installer creates this file from the template.
2. Generate two independent secrets locally. Standalone operators do not need Python.

   Windows PowerShell (run twice, once for each secret):

   ```powershell
   $bytes = New-Object byte[] 32
   $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
   $rng.GetBytes($bytes)
   $rng.Dispose()
   [Convert]::ToBase64String($bytes).Replace('+','-').Replace('/','_')
   ```

   Linux with OpenSSL (run twice):

   ```sh
   openssl rand -base64 32 | tr '+/' '-_'
   ```

   Developers with Python may instead use:

   ```sh
   python -c "import secrets; print(secrets.token_urlsafe(32))"
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```

   Put the first in `CHEFAS_PROXY_ADMIN_TOKEN` and the second in `CHEFAS_PROXY_SECRETS_KEY`. Preserve the encryption key in a private backup; losing it prevents recovery of encrypted pool passwords. Do not send these values to support or add them to Git.
3. Set `CHEFAS_PROXY_DEVFEE_PASSWORD` if your fee account requires it. The default pool password `x` is public convention, not the administrative secret. User credentials normally come from each ASIC; the authenticated Routes form can override them.

Run `start.cmd` from the Windows standalone archive, or start the installed Ubuntu service with `sudo systemctl enable --now chefas-proxy`. For a manual Linux launch from the archive, run `./start.sh`. The root scripts prefer the matching standalone package in `dist/`, otherwise select `bin/chefas-proxy/`; a distributed bundle's scripts select their own directory. `CHEFAS_PROXY_PYTHON` can specify an absolute interpreter path. Missing binaries cause an error; the production scripts do not silently fall back to source.

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

**Give someone read-only access** from **Routes → Create Observer Link**.
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
