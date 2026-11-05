# Product roadmap

Current operator documentation: [Quick start](../quickstart.md), [compatibility](../compatibility.md). Historical stage descriptions are preserved in [operations reference](operations-reference.md).

Implemented: active Stratum routing, exact accepted-work ledger, independent fee channels, jitter and health policy, local PoW gate, job TTL, verified TLS, bounded framing, miner Reaper, history, protected management API, Observer Links and native builds. Product packaging and conservative compatibility profiles are now included.

## Remaining release gates

0. **Fleet fee allocation:** the current process-wide `fee_owner` serializes fee mining. With more than 100 identical active miners, one miner cannot supply 1% of the farm's work; variable hashrates require the same capacity analysis. Replace serial ownership with bounded concurrent allocation, preserve accepted-difficulty reservations/in-flight accounting, and test fairness across accounts and reconnects. The single-miner 990/10 test does not prove fleet billing.

1. **Hardware pilot:** exact Antminer/Whatsminer/Avalon models and stock/Vnish/Braiins builds, including varying nonce widths, masks and hot-change reactions. No firmware family is certified by its user-agent string.
2. **Protocol breadth:** pool-initiated RPC (`client.get_version` and response correlation), uncommon extensions and validation of all pool job fields before publication. Unknown methods currently meet a strict allowlist; do not claim universal compatibility.
3. **Availability:** long-duration lossy 4G tests, repeated DNS changes and restoration, independent backup credentials, reconnect storms and event-loop latency under local PoW validation.
4. **Measured capacity:** CPU, RSS, descriptors, bandwidth and accepted/rejected work at increasing real traffic loads. 1000–5000 ASICs is an unmeasured target, not an advertised capability.
5. **Distribution:** native Linux build, signed artifacts/update verification, reproducible release procedure, service installation and rollback, then independent review.
6. **Product:** refine the dashboard from operator feedback and choose the final name. Aggregation/Stratum V2 or other algorithms require separate design; the current scope is BTC SHA-256d Stratum V1.

A 5 MW farm is a deployment context, not a test result. Device count, firmware mix, uplink and pool policy must be measured before scaling a pilot.
