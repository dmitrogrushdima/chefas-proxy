# Operator quick start

Use a trusted standalone build made for the pilot machine: it bundles Python and the native core. Follow the [three-step package setup](release.md#operator-three-steps). Only the separate native development runtime needs an installed matching Python; see [development](development.md).

## Configure the server

1. Copy `.env.example` to `.env` in the directory from which you run the launcher. ENV values override this explicit file.
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

Run `start.bat` on Windows or `./start.sh` on Linux. The root scripts prefer the matching standalone package in `dist/`, otherwise select `bin/chefas-proxy/`; a distributed bundle's scripts select their own directory. `CHEFAS_PROXY_PYTHON` can specify an absolute interpreter path. Missing binaries cause an error; the production scripts do not silently fall back to source.

## Pool and miner addresses

The default miner listener is `127.0.0.1:3333`. For a farm LAN, set `--listen-host 0.0.0.0 --allow-cidr YOUR_LAN_CIDR`; allow only the intended network through the host firewall. The dashboard stays on `127.0.0.1:8080`. For a remote server, access it through an SSH tunnel, for example `ssh -L 8080:127.0.0.1:8080 USER@SERVER`.

`--pool-host` and `--devfee-host` accept bare hosts with separate port flags or full `stratum+ssl://HOST:PORT` URLs. SSL URLs verify the certificate and hostname. `--tls-ca-file PATH` supplies a private CA; it does not disable verification. `--tls` forces TLS for every upstream; omit that global flag when intentionally mixing TCP and SSL routes.

Configure each pilot ASIC with:

| ASIC field | Value |
| --- | --- |
| Pool URL | `stratum+tcp://PROXY_LAN_IP:3333` |
| Worker | Its normal **user-pool** account/worker name |
| Password | Its normal user-pool password |

The ASIC-facing connection is TCP, even when upstreams use TLS. Preserve a direct-pool backup entry on the ASIC according to the farm operator's availability policy. The proxy's `--backup-host/--backup-port` is a separate upstream failover mechanism.

Enable `--active-router --validate-pow` for stateful routing and local PoW checks. Configure all three fee fields (`--devfee-host`, port or URL port, `--devfee-worker`) when using DevFee. `--require-devfee` explicitly stops mining after a sustained fee outage; restarting a still-broken configuration does not fix that outage.

## Verify before expanding

Check accepted shares at **both pools**, compare pool rejects, inspect the audit export and confirm worker identity. Measure reconnects and hashrate over complete windows; an estimate during warmup is not farm capacity. Test primary failure, restoration and an orderly shutdown on the actual OS. Record ASIC model, firmware version and pool endpoint in the [compatibility matrix](compatibility.md).

If chips disconnect after hot changes, use `--hot-switch-mode reconnect` while investigating. Automatic fallback profiles are local, expire after 24 hours and can be reset by stopping the process and removing the chosen compatibility-state file. Start with conservative defaults; do not copy zero-jitter settings from laboratory tests.

State lives in `state/`, rotated logs in `logs/`. Restrict both directories and `.env` to the service account. SIGINT/SIGTERM perform orderly cleanup; a forced kill can lose the last checkpoint. The UI shows only measured data: ASIC network RTT is not invented from mining time.

Defaults currently allow 512 TCP connections and retain at most 512 worker-history identities. Raising `--max-connections` alone does not enlarge the history table. A multi-thousand-device rollout requires capacity and telemetry scaling work; do not infer support from a higher numeric connection limit.
