# Product roadmap

Current operator documentation: [Quick start](../quickstart.md), [compatibility](../compatibility.md). Historical stage descriptions are preserved in [operations reference](operations-reference.md).

Implemented: active Stratum routing, exact accepted-work ledger, concurrent fee allocation with in-flight reservations, independent fee channels, jitter and health policy, local PoW gate, job TTL, verified TLS, bounded framing, miner Reaper, history, protected management API, Observer Links and native builds. Standalone packaging, async DNS/backpressure and conservative compatibility profiles are included. The aggregation math is tested separately from live routing.

## Remaining release gates

0. **Connection consolidation:** connect the tested nonce partitioner/local vardiff model to a shared user/fee upstream lifecycle. Prove account isolation, group reconnects, immutable job context and separate local/pool acknowledgements. Independent upstream sessions currently remain the live transport model. See [fleet scaling](fleet-scaling.md).

1. **Hardware pilot:** exact Antminer/Whatsminer/Avalon models and stock/Vnish/Braiins builds, including varying nonce widths, masks and hot-change reactions. No firmware family is certified by its user-agent string.
2. **Protocol breadth:** pool-initiated RPC (`client.get_version` and response correlation), uncommon extensions and validation of all pool job fields before publication. Unknown methods currently meet a strict allowlist; do not claim universal compatibility.
3. **Availability:** long-duration lossy 4G tests, dual-stack Happy Eyeballs, repeated DNS changes and restoration, independent backup credentials, reconnect storms and event-loop latency under local PoW validation. Review the required-fee watchdog's process-wide blast radius for mixed fleets.
4. **Measured capacity:** CPU, RSS, descriptors, bandwidth and accepted/rejected work at increasing real traffic loads. 1000–5000 ASICs is an unmeasured target, not an advertised capability.
5. **Distribution:** qualify the standalone Linux build and generated systemd unit on Linux, sign artifacts/update verification, lock the complete build toolchain, test service installation/rollback, then independent review. Windows standalone smoke checks do not prove Linux compatibility or byte-for-byte reproducibility.
6. **Product:** refine the dashboard from operator feedback and choose the final name. Aggregation/Stratum V2 or other algorithms require separate design; the current scope is BTC SHA-256d Stratum V1.

A 5 MW farm is a deployment context, not a test result. Device count, firmware mix, uplink and pool policy must be measured before scaling a pilot.
