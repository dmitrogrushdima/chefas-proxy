# KONTUR · ASIC Mining Proxy

**One ASIC endpoint. Flexible pool routing. An auditable 1% DevFee target.**

KONTUR is the working name of our Bitcoin / SHA-256d Stratum V1 proxy. It connects ASIC miners to their pools and gives farm operators a local control panel, per-worker statistics and a transparent accepted-work ledger.

**Pilot candidate.** Synthetic PoW, network and native-build tests are available. Real Antminer, Whatsminer, Avalon and custom-firmware compatibility is being qualified; there is no claim of universal support or certified farm capacity.

## What operators get

- Independent user/DevFee connections, verified TLS URLs and controlled failover.
- Exact version-rolling negotiation, extranonce translation and remembered reconnect fallback.
- Optional local PoW/duplicate filtering, bounded parsing, job TTL and miner liveness checks.
- Gradual DevFee connection startup and accepted-difficulty accounting with JSON export.
- Concurrent fee windows with a shared difficulty reservation budget and explicit session worker templates.
- Live worker tables, rolling hashrate, history checkpoints and read-only Observer Links.
- Authenticated route changes, Kick and IP/CIDR blacklist. English, Russian and Chinese UI.

## Run the compiled version in three steps

### 1. Prepare a matching build

There is no public signed installer yet. Maintainers can create a **standalone Windows x64 or Linux x64** package with the release scripts; operators of that package do not need Python. See the [standalone build and launch guide](docs/release.md).

For a native development runtime, use Python 3.10+ and a C compiler on the target platform:

```sh
python -m pip install -r requirements-build.txt
python build_ext.py
```

The source-free runtime is created in `bin/kontur/`. Windows `.pyd` and Linux `.so` builds require matching OS, architecture and Python ABI. [Build details](docs/development.md).

### 2. Configure access and fee credentials

Copy `.env.example` to a private `.env`. Set independent `KONTUR_ADMIN_TOKEN` and `KONTUR_SECRETS_KEY` values and any required pool passwords. Keep the key outside release archives. [Exact setup](docs/quickstart.md).

### 3. Start the proxy and connect a pilot ASIC

Windows example — replace the pool/account and LAN values:

```bat
start.bat --pool-host stratum+ssl://USER_POOL:443 --devfee-host stratum+ssl://FEE_POOL:443 --devfee-worker FEE_ACCOUNT.worker --active-router --validate-pow --require-devfee --listen-host 0.0.0.0 --allow-cidr 192.168.10.0/24
```

Linux: use `./start.sh` with the same arguments. Open **http://127.0.0.1:8080/** and sign in. Point the ASIC at **stratum+tcp://PROXY_LAN_IP:3333** with its usual pool worker credentials. Start with a small pilot, then expand after measuring pool rejects and reconnects. [Connection guide and troubleshooting](docs/quickstart.md).

## Transparent commission and protection

The fixed **1% target uses pool-confirmed difficulty**, not a count of shares or a promise about monetary payouts. Whole shares, warmup and outages create visible short-term deviations. The ledger exposes accepted user/fee work and the outstanding difference.

The scheduler permits parallel fee windows and reserves expected difficulty before assignment, so miners share one commission budget. Late proofs retain their original route; unknown pool outcomes are reported separately. `--devfee-worker-template '{worker}_{slot}'` optionally gives independent sessions distinct worker names. Jitter and connection admission reduce synchronization; they cannot promise a smooth stochastic hashrate graph.

Live routing still uses independent upstreams per ASIC. The aggregation mathematics is implemented and tested, but shared upstream socket consolidation is **not enabled**. No 95% bandwidth saving or 5,000-device capacity is claimed. [Scaling design and remaining gates](docs/engineering/fleet-scaling.md).

`--require-devfee` enables the commercial watchdog: sustained fee failure closes sessions and blocks new mining until restart. It is an explicit availability tradeoff. Cython makes source modification harder; it cannot prevent an owner of the machine from altering the program.

## Project layout

```text
src/          Stratum core, session lifecycle, API and observation
dashboard/    Local HTML/CSS/JS interface
tests/        Protocol, security and live PoW regressions
lab/          Synthetic pools and load fixtures
docs/         Operator guides and engineering references
bin/          Ignored native builds; never real secrets
scripts/      Development helpers
```

[Operator guide](docs/quickstart.md) · [Compatibility and pilot matrix](docs/compatibility.md) · [Build and tests](docs/development.md) · [Documentation index](docs/README.md)
