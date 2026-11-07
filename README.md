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

[Quick start](#running-in-three-steps) · [Compatibility](#pools--hardware) · [Documentation](docs/README.md) · [Support development](#support-development)

</div>

**CHEFAS Proxy** sits between your Bitcoin ASICs and mining pools. Route connections, follow worker activity and inspect the DevFee ledger from a local dashboard. Built exclusively for **BTC · SHA-256d · Stratum V1**.

> **Pilot candidate:** Windows standalone packaging has been tested. Linux deployment and physical ASIC/pool combinations still need qualification. Public downloads appear on Releases when published; this README does not announce a signed production release.

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

## Running in three steps

### Windows x64

1. **Unpack** a matching CHEFAS Proxy release ZIP. Keep `chefas-proxy.exe` beside its `_internal` folder.
2. **Configure** `chefas-proxy.example.json` → `chefas-proxy.json` with your pool and permitted farm network; `.env.example` → `.env` with your own admin token and secrets. [Configuration guide →](docs/quickstart.md)
3. **Run** `chefas-proxy.exe` (or `start.cmd` to keep startup errors visible). Open **http://127.0.0.1:8080**.

### Linux x64 · pilot build target

1. **Unpack** a matching Linux build into `/opt/chefas-proxy`.
2. **Configure and install** the supplied service template, service account and private environment file. [Exact systemd setup →](docs/release.md#linux-service)
3. **Start** with `sudo systemctl enable --now chefas-proxy`. Access the dashboard locally or through an SSH tunnel.

**Point your ASIC here:** `stratum+tcp://PROXY_LAN_IP:3333`. Keep its normal user-pool worker name and password. Start with one pilot miner, compare accepted work at the pool, then expand.

No separate Python installation is needed for a standalone package. Building from source is covered in the [release guide](docs/release.md).

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

<sub>Pool icons: <a href="https://github.com/mempool/mining-pool-logos">mempool mining pool logos</a>. Trademarks belong to their respective owners.</sub>

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

**Current boundary:** each ASIC still has independent upstream connections. Aggregation mathematics is tested, but shared TCP consolidation is not enabled. There is no advertised 95% bandwidth saving, automatic ASIC overclock or certified 5,000-device capacity. [Engineering roadmap →](docs/engineering/roadmap.md)

## Community

Join [CHEFAS Proxy News](https://t.me/Chefasproxy) for announcements or [the community chat](https://t.me/Chefasproxychat) for discussion. Use [GitHub Issues](https://github.com/dmitrogrushdima/chefas-proxy/issues) for reproducible bug reports; remove credentials and identifying network details from traces.

## Support development

Voluntary donations support CHEFAS Proxy development and are separate from DevFee.

| | Currency | Network | Address |
| :---: | :--- | :--- | :--- |
| <img src="https://cdn.jsdelivr.net/npm/cryptocurrency-icons@0.18.1/svg/color/btc.svg" align="absmiddle" width="24" height="24" alt=""> | **Bitcoin (BTC)** | BTC | `bc1qkwkjk0cgu6zdawx6j5tqlqzl4dcftvt3408rzx` |
| <img src="https://cdn.jsdelivr.net/npm/cryptocurrency-icons@0.18.1/svg/color/usdt.svg" align="absmiddle" width="24" height="24" alt=""> | **Tether (USDT)** | TRON · TRC-20 | `TPZyEeqNMv9yZmAZWKYnDUkz3sJKxnK8YM` |
| <img src="https://cdn.jsdelivr.net/npm/cryptocurrency-icons@0.18.1/svg/color/sol.svg" align="absmiddle" width="24" height="24" alt=""> | **Solana (SOL)** | Solana | `CDY7UUREaDZz56ss4yXfpWvovrw53dSeWyD5koW9y5gN` |

<sub>Icons: <a href="https://github.com/spothq/cryptocurrency-icons">Cryptocurrency Icons</a> · CC0 · served by jsDelivr.</sub>

## Documentation & source

[`src/`](src) core · [`dashboard/`](dashboard) interface · [`tests/`](tests) regressions · [`lab/`](lab) synthetic test pools · [`docs/engineering/`](docs/engineering) design and limits.

[Operator guide](docs/quickstart.md) · [Build & release](docs/release.md) · [Development](docs/development.md) · [Upgrade naming](docs/engineering/branding.md)

## License

CHEFAS Proxy is available under the [MIT License](LICENSE). Bundled dependencies retain their own license terms.
