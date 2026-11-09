<div align="center">

![CHEFAS Proxy — Mining, with clarity.](docs/assets/chefas-proxy.svg)

**Bitcoin mining, with a clearer view of every connection.**

[![Version](https://img.shields.io/badge/version-0.1.0rc1-cb8b5f?style=flat-square)](docs/release.md)
[![Stage](https://img.shields.io/badge/stage-pilot_candidate-64748b?style=flat-square)](docs/compatibility.md)
[![Windows](https://img.shields.io/badge/Windows-x64-334155?style=flat-square)](docs/release.md)
[![Linux](https://img.shields.io/badge/Linux-x64_build_target-334155?style=flat-square&logo=linux&logoColor=white)](docs/release.md#linux-service)
[![License](https://img.shields.io/badge/license-MIT-green?style=flat-square)](#license)

[![Releases](https://img.shields.io/badge/GitHub-Releases-181717?style=flat-square&logo=github)](https://github.com/dmitrogrushdima/chefas-proxy/releases)
[![Telegram](https://img.shields.io/badge/Telegram-News-229ED9?style=flat-square&logo=telegram&logoColor=white)](https://t.me/Chefasproxy)
[![Community](https://img.shields.io/badge/Telegram-Community-229ED9?style=flat-square&logo=telegram&logoColor=white)](https://t.me/Chefasproxychat)

**Installation:** [English](#install-on-your-farm) · [Русский](docs/install.ru.md) · [简体中文](docs/install.zh-CN.md)

[Quick start](#install-on-your-farm) · [Web dashboard](#open-the-dashboard) · [Compatibility](#pools--hardware) · [Documentation](docs/README.md) · [Support development](#support-development)

</div>

**CHEFAS Proxy** sits between your Bitcoin ASICs and mining pools. Route connections, follow worker activity and inspect the DevFee ledger from a local dashboard. Built exclusively for **BTC · SHA-256d · Stratum V1**.

> **Pilot candidate.** Start with one ASIC and confirm that your pool accepts shares before expanding.

## Install on your farm

Install CHEFAS on a computer or server reachable from the ASICs over your farm network. Each farm runs its own proxy and dashboard.

Open [GitHub Releases](https://github.com/dmitrogrushdima/chefas-proxy/releases) and
look under **Assets** for your operating system.

| Farm computer | Download |
|---|---|
| Windows x64 | `chefas-proxy-windows-x64.zip` |
| Ubuntu 24.04 x64 | `chefas-proxy-linux-x64.tar.gz` |

**If Releases is empty, request the standalone pilot archive for your OS from the maintainer.** GitHub's **Code → Download ZIP** and
**Source code** assets contain the developer checkout. The standalone archives
already include the runtime and dashboard. Keep the executable and `_internal`
together after extraction.

### Windows

1. Extract the ZIP and double-click `start.cmd`. It opens `chefas-proxy.json`.
2. Set `pool_host` to your current BTC pool URL, including its port. For example, `"pool_host": "stratum+tcp://btc.viabtc.io:3333"`. Keep the rest of the JSON and save.
3. Copy `.env.example` to `.env` beside the JSON and set the dashboard credentials below. Run `start.cmd` again. Keep the window open and copy the `ASIC URL` shown in the console.

### Ubuntu

From the directory containing the archive, run:

```sh
tar -xzf chefas-proxy-linux-x64.tar.gz
cd chefas-proxy-linux-x64
sudo bash install.sh
sudoedit /etc/chefas-proxy/chefas-proxy.json
```

Set `pool_host` to your BTC pool URL, including its port. Save the JSON. Run
`sudoedit /etc/chefas-proxy/chefas-proxy.env` and set the dashboard credentials
below. Then start the service and copy the `ASIC URL` for your farm network:

```sh
sudo systemctl enable --now chefas-proxy
sudo journalctl -u chefas-proxy -n 30 --no-pager
```

<details>
<summary>Set the dashboard credentials</summary>

Generate two separate values. Use the first for `CHEFAS_PROXY_ADMIN_TOKEN`
and the second for `CHEFAS_PROXY_SECRETS_KEY`. Remove the leading `#` on these
two lines in the environment file, keep the trailing `=` of each generated
value, and store the file privately.

**Windows PowerShell** (run twice):

```powershell
$bytes = New-Object byte[] 32
$rng = [Security.Cryptography.RandomNumberGenerator]::Create()
$rng.GetBytes($bytes)
$rng.Dispose()
[Convert]::ToBase64String($bytes).Replace('+','-').Replace('/','_')
```

**Ubuntu** (run twice):

```sh
openssl rand -base64 32 | tr '+/' '-_'
```

</details>

### Connect one ASIC first

| ASIC setting | Value |
| --- | --- |
| **Pool 1 URL** | Copy the farm's `ASIC URL` from the startup output, for example `stratum+tcp://192.168.1.50:3333`. |
| **Worker and password** | Keep the values already used with your BTC pool. |
| **Pool 2 URL** | Keep the direct BTC pool address as a fallback, with its existing credentials. |

Apply the settings and check that **Pool 1** is active. In CHEFAS, confirm the
worker appears and accepted shares increase. Check the pool's worker page
before adding more ASICs. To roll back, restore the original Pool 1 settings.

### Open the dashboard

**On the proxy computer:** open `http://127.0.0.1:8080` and sign in with
`CHEFAS_PROXY_ADMIN_TOKEN`.

**On another computer:** if the proxy host has SSH access, run the following
in PowerShell or a terminal. Replace `operator` and `192.168.1.50` with
your SSH username and the proxy host's reachable address.

```sh
ssh -N -o ExitOnForwardFailure=yes -L 127.0.0.1:18080:127.0.0.1:8080 operator@192.168.1.50
```

Keep the terminal open and visit `http://127.0.0.1:18080`. The ASIC uses
Stratum on port **3333**. The browser uses the dashboard on port **8080**
on the proxy host, or **18080** through the tunnel.

**Read-only access:** sign in as admin and choose **Routes → Create Observer
Link**. The link expires after one hour and works for recipients who can reach
the dashboard. A link using a loopback address requires their own tunnel on
the same local port.

[Operator guide and troubleshooting](docs/quickstart.md) · [Release checksums](docs/release.md)

## Built around your farm

| Capability | What you get |
| :--- | :--- |
| **Clear operations** | Live worker tables, estimated hashrate, accepted/rejected shares and history checkpoints. |
| **Flexible routing** | User and DevFee upstreams, verified TLS, failover and controlled route changes. |
| **Visible 1% target** | Pool-confirmed difficulty accounting, outstanding DevFee balance and JSON export. |
| **Careful switching** | Hot extranonce changes on capable firmware; controlled reconnect fallback otherwise. |
| **Local protection** | Optional PoW checks, duplicate filtering, bounded buffers, job expiry and idle-session cleanup. |
| **Private administration** | Admin authentication, read-only Observer Links, Kick and IP/CIDR blocking. |

Dashboard languages: **English · Русский · 简体中文**. No cloud account is needed for the local dashboard.

## Pools & hardware

Configure a Bitcoin Stratum V1 endpoint for your pool. These are examples, not endorsements or verified partnerships.

<table>
<tr>
<td align="center" width="25%"><a href="https://www.f2pool.com"><img src="https://cdn.jsdelivr.net/gh/mempool/mining-pool-logos@059212bff7194617153c4307bc7acfccea057b6c/f2pool.svg" width="36" height="36" alt=""><br><strong>f2pool</strong></a><br><sub>Bitcoin · Stratum V1</sub></td>
<td align="center" width="25%"><a href="https://www.viabtc.com"><img src="https://cdn.jsdelivr.net/gh/mempool/mining-pool-logos@059212bff7194617153c4307bc7acfccea057b6c/viabtc.svg" width="36" height="36" alt=""><br><strong>ViaBTC</strong></a><br><sub>Bitcoin · Stratum V1</sub></td>
<td align="center" width="25%"><a href="https://www.antpool.com"><img src="https://cdn.jsdelivr.net/gh/mempool/mining-pool-logos@059212bff7194617153c4307bc7acfccea057b6c/antpool.svg" width="36" height="36" alt=""><br><strong>AntPool</strong></a><br><sub>Bitcoin · Stratum V1</sub></td>
<td align="center" width="25%"><a href="https://www.poolin.com"><img src="https://cdn.jsdelivr.net/gh/mempool/mining-pool-logos@059212bff7194617153c4307bc7acfccea057b6c/poolin.svg" width="36" height="36" alt=""><br><strong>Poolin</strong></a><br><sub>Bitcoin · Stratum V1</sub></td>
</tr>
<tr>
<td align="center" width="25%"><a href="https://pool.braiins.com"><img src="https://cdn.jsdelivr.net/gh/mempool/mining-pool-logos@059212bff7194617153c4307bc7acfccea057b6c/braiinspool.svg" width="36" height="36" alt=""><br><strong>Braiins Pool</strong></a><br><sub>Bitcoin · Stratum V1</sub></td>
<td align="center" width="25%"><a href="https://foundrydigital.com"><img src="https://cdn.jsdelivr.net/gh/mempool/mining-pool-logos@059212bff7194617153c4307bc7acfccea057b6c/foundryusa.svg" width="36" height="36" alt=""><br><strong>Foundry USA</strong></a><br><sub>Bitcoin · Stratum V1</sub></td>
<td align="center" colspan="2"><strong>Your BTC pool</strong><br><sub>Custom host · TCP / TLS</sub></td>
</tr>
</table>

### ASIC hardware

| Family | Bitcoin models | Firmware qualification |
| :--- | :--- | :--- |
| **Bitmain Antminer** | S19 / S21 series | Stock, Vnish and Braiins OS+ builds require model-specific pilot verification |
| **MicroBT Whatsminer** | M30 / M50 / M60 series | Stock firmware qualification pending |
| **Canaan Avalon** | SHA-256d models | Exact model and stock firmware qualification pending |
| **Innosilicon** | Bitcoin SHA-256d models | Exact model and firmware qualification pending |

Protocol capabilities include version rolling and declared extranonce updates. Physical model, firmware and public-pool combinations remain under qualification. [Compatibility matrix](docs/compatibility.md).

## A commission you can inspect

The **1% DevFee target is measured in pool-confirmed difficulty**, not raw share count or monetary payouts. Parallel fee sessions reserve work against one shared budget; the dashboard exposes accepted user/fee work and the remaining balance. Whole shares and network outages can cause short-term deviations.

`--require-devfee` enables the watchdog: sustained fee failure stops mining until restart. Configure this availability policy deliberately. Compilation does not make a machine owner's modifications impossible.

**Shared upstream pilot:** `--active-router --aggregate` groups compatible ASICs on shared TCP connections and supports version rolling, automatic pool failover and optional DevFee. [Pilot configuration and limits →](docs/engineering/fleet-scaling.md)

## Community

Join [CHEFAS Proxy News](https://t.me/Chefasproxy) for announcements or [the community chat](https://t.me/Chefasproxychat) for discussion. Use [GitHub Issues](https://github.com/dmitrogrushdima/chefas-proxy/issues) for reproducible bug reports; remove credentials and identifying network details from traces.

## Support development

Voluntary donations support CHEFAS Proxy development and are separate from DevFee.

| | Currency | Network | Address |
| :---: | :--- | :--- | :--- |
| <img src="https://cdn.jsdelivr.net/npm/cryptocurrency-icons@0.18.1/svg/color/btc.svg" align="absmiddle" width="24" height="24" alt=""> | **Bitcoin (BTC)** | BTC | `bc1qkwkjk0cgu6zdawx6j5tqlqzl4dcftvt3408rzx` |
| <img src="https://cdn.jsdelivr.net/npm/cryptocurrency-icons@0.18.1/svg/color/usdt.svg" align="absmiddle" width="24" height="24" alt=""> | **Tether (USDT)** | TRON · TRC-20 | `TPZyEeqNMv9yZmAZWKYnDUkz3sJKxnK8YM` |
| <img src="https://cdn.jsdelivr.net/npm/cryptocurrency-icons@0.18.1/svg/color/sol.svg" align="absmiddle" width="24" height="24" alt=""> | **Solana (SOL)** | Solana | `CDY7UUREaDZz56ss4yXfpWvovrw53dSeWyD5koW9y5gN` |


## Documentation & source

[`src/`](src) core · [`dashboard/`](dashboard) interface · [`tests/`](tests) regressions · [`lab/`](lab) synthetic test pools · [`docs/engineering/`](docs/engineering) design and limits.

[Operator guide](docs/quickstart.md) · [Build & release](docs/release.md) · [Development](docs/development.md) · [Upgrade naming](docs/engineering/branding.md)

## License

CHEFAS Proxy is available under the [MIT License](LICENSE). Bundled dependencies retain their own license terms.
