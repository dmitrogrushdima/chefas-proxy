# KONTUR · ASIC Mining Proxy

**One ASIC endpoint. Flexible pool routing. An auditable 1% DevFee target.**

KONTUR is the working name of our Bitcoin / SHA-256d Stratum V1 proxy. It connects ASIC miners to their pools and gives farm operators a local control panel, per-worker statistics and a transparent accepted-work ledger.

**Pilot candidate.** Synthetic PoW, network and native-build tests are available. Real Antminer, Whatsminer, Avalon and custom-firmware compatibility is being qualified; there is no claim of universal support or certified farm capacity.

## What operators get

- Independent user/DevFee connections, verified TLS URLs and controlled failover.
- Exact version-rolling negotiation, extranonce translation and remembered reconnect fallback.
- Optional local PoW/duplicate filtering, bounded parsing, job TTL and miner liveness checks.
- Gradual DevFee connection startup and accepted-difficulty accounting with JSON export.
- Live worker tables, rolling hashrate, history checkpoints and read-only Observer Links.
- Authenticated route changes, Kick and IP/CIDR blacklist. English, Russian and Chinese UI.

## Run the compiled version in three steps

### 1. Prepare a matching native build

There is no public signed installer yet. From a trusted checkout on the target platform, install Python 3.10+ and a C compiler, then:

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

Large-fleet billing is still a release gate: the current scheduler serializes fee-window ownership. One fee session can be insufficient to supply 1% of a large farm's total work. Multi-session fee allocation and fairness need dedicated validation before a whole-farm rollout.

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
