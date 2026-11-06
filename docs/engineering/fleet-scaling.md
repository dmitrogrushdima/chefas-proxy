# Fleet scaling: implementation and release gates

This work extends the Stratum V1 pilot. It does not certify commercial pools,
firmware, 5,000-device capacity or a fixed percentage of bandwidth savings.

## Implementation order

1. **Network isolation** — `network_io.py`, `upstream_manager.py`, `router.py`:
   bounded asynchronous DNS, per-transport backpressure and urgent clean jobs.
2. **Aggregation mathematics** — `aggregation.py`: immutable group identity,
   nonoverlapping nonce ranges, job-bound difficulty and bounded local vardiff.
3. **Concurrent fee allocation** — `devfee_scheduler.py`, `router.py`:
   reserve expected difficulty before admitting independent fee windows and
   account for confirmations and late submissions separately.
4. **Distribution** — release scripts: compile extensions, package a standalone
   runtime on the target OS, hash the result and generate a systemd unit.
5. **Shared user upstream integration** — connect the aggregation model to a
   shared connection lifecycle, then test reconnects, group isolation, job
   fanout, rejected shares and per-device observation before enabling it.

## Network invariants

The previous `asyncio` resolver already ran system DNS in an executor; it did
not deliberately execute blocking DNS on the event loop. The new c-ares
resolver additionally avoids exhausting that shared executor during DNS
outages. Positive/negative caches, pending lookups and returned addresses are
bounded. Concurrent lookups for one endpoint share work; cancelling one caller
does not cancel all callers. TLS keeps certificate and hostname verification.

Each writer has 64 KiB / 16 KiB high/low watermarks, a 256 KiB application
write ceiling and a bounded drain deadline. A stalled upstream pauses its
source ASIC transport while draining. Other ASICs keep their own tasks and
budgets. These limits exclude kernel buffers and the TLS implementation.

Pool readers do not wait on the ASIC notification writer. Two latest-job
slots and a bounded control queue decouple them. Clean jobs replace obsolete
queued jobs and cancel an uncommitted fee transition. An extranonce/difficulty/
notify batch already written to TCP cannot be preempted or reordered safely.
The next clean job is delivered under its own issuing context.

Independent upstream subscriptions retain the ASIC's exact first subscribe
parameter (agent/version) and omit another pool's session-resumption ID.
This is protocol fidelity, not invisibility or avoidance of pool rules.

The numeric destination is checked again before connecting, closing the gap
between a hostname preflight and a new TTL-zero DNS answer. Address attempts
currently share one total timeout and run sequentially; a blackholed first
address can exhaust it. Happy Eyeballs is not implemented yet.

## Live fee allocator

`FairnessAllocator` replaces exclusive global window ownership. An assignment
reserves one expected share at the fee job's actual difficulty. Submitted work
becomes an independent in-flight reservation; releasing a mining window never
forgets its late proofs. Positive pool responses update the ledger before the
reservation is settled and before the ASIC acknowledgement is written.
Timeouts and ambiguous outcomes are counted as unknown work, never retried or
reported as confirmed acceptance. The final indivisible share and valid late
proofs can exceed a short-term target; excess remains visible and compensated.

A single fleet timer revisits idle fee windows and admits bounded batches of
candidates in rotating order. Per-session reconciliation is serialized and
does not switch while that miner's submit RPC is pending. Session slots are
bounded, unique while in use, and reusable only after their channels close.
The explicit `--devfee-worker-template '{worker}_{slot}'` can give each
independent upstream a distinct worker name; the default preserves the exact
configured account. This is concurrent independent routing, not pooled fee
TCP connections or hashrate aggregation.

The required-fee watchdog still has process-wide scope by design. A sustained
failure affecting one session, including a fee-route compatibility problem,
can stop the entire process's mining sessions. Qualify a mixed fleet and its
fee route before enabling `--require-devfee` in a large deployment.

## Aggregation invariants

Only sessions with the same endpoint, TLS policy, account credentials and
negotiated protocol constraints may share an upstream. Different customer
wallets must never be silently merged. A pool sees the shared upstream worker;
device-level attribution must remain local and be labeled accordingly.

Nonce partitioning must preserve the coinbase bytes exactly:

`upstream_extranonce1 + prefix + local_extranonce2`

equals `downstream_extranonce1 + local_extranonce2`. A prefix is never reused
within the same upstream session. When upstream extranonce2 has no room for a
prefix compatible with the ASIC's minimum width, use independent sessions.
Changing a width requires a new mining context; padding a submit after hashing
would invalidate the proof.

Small shares cannot be combined into a Bitcoin proof. Local vardiff yields
local proofs for feedback; only an independently pool-target-valid proof or a
network block candidate may be submitted upstream. Local acknowledgements are
not pool acceptance and must not enter the DevFee ledger or payout estimates.

## Qualification required before fleet rollout

- Live consolidation must prove that multiple ASIC sockets use one user
  upstream, with no nonce collisions, across reconnect and version negotiation.
- Validate heterogeneous difficulty and hashrate, unavailable fee pools,
  delayed/unknown acknowledgements and late jobs without replaying submissions.
- Measure FD counts, memory, event-loop latency and WAN bytes with the proxy
  inside the farm LAN. A remote VPS does not remove ASIC-to-VPS WAN traffic.
- Build and test Linux artifacts on Linux; a Windows build is not a Linux test.
- Run real Antminer/Whatsminer firmware and a long lossy-network soak. Synthetic
  passing tests are evidence of the tested invariants, not hardware certification.

## Validation on Windows x64 / CPython 3.10 — 2026-11-06

- Final source suite: **231 passed, 123 subtests passed**, with the 400-client
  regression selected separately to isolate load from other tests/builds.
- Final compiled core: **54 passed, 42 subtests passed**, including the original
  400-client isolation regression and its unchanged 20-second response deadline.
  All 17 modules were preloaded from the final standalone `_internal` directory
  and their native import paths checked before pytest.
- Final standalone executable: **36 live integration tests passed** through
  `chefas-proxy.exe`; two in-process shutdown tests were covered by the source suite.
  Coverage includes 1000 valid shares (990 user / 10 fee), simultaneous fee
  windows (198 user / 2 fee), and a fee pool four times harder than the user
  pool (396 user / 1 fee share, 1% of accepted difficulty), with no pool rejects
  in those scenarios. TLS, late jobs, failover, route reload and liveness paths
  are included in this run.
- The initial compiled load attempt exposed per-packet Task/Future overhead.
  Standard unpaused `StreamWriter` drains now run inline; paused/custom drains
  retain their bounded cancellation-safe path. Seven real-socket tests verify
  timeout, cancellation, connection errors and monkeypatched-method fallback.
- The standalone contains 17 native modules. Source hashes, 265 release-file
  hashes, ZIP CRC and archive SHA-256 were checked. Clean-environment `--help`
  and `--release-info` passed with developer Python paths removed.

The Windows archive SHA-256 for this build is
`6bd893d7685a6aee64721fbbd9689a3ed0ba746f90c8078f8bc71a0d76fa534d`.
These are local regression results, not a farm throughput benchmark. The
generated Linux build/service still needs qualification on Linux.


## Pilot telemetry and TP mode

`TP_MODE=true` in the server environment or `"TP_MODE": true` in the standalone JSON configuration selects raw forwarding. The CLI equivalent is `--tp-mode`. The authenticated `POST /api/mode` accepts `{"tp_mode": true}` and persists the selection for new connections. Existing sessions retain their admission mode. `/api/config` and live status expose the selected mode.

TP sessions bypass Stratum parsing, local PoW checks and DevFee routing. Basic connection admission, deadlines and backpressure still apply. Disabling TP restores the configured routing mode for subsequent connections.

`coalesced_read_batches` counts socket reads containing multiple complete JSON messages. `coalesced_extra_messages` counts the additional messages in those batches. These describe application reads, whose boundaries can differ from TCP packets. Both counters and `compatibility_fallbacks` appear in the metrics API and minute-summary logs. Learning a compatibility fallback emits `reconnect_fallback`.

Shared upstream TCP consolidation and socket-preserving route migration are still release gates. The aggregation primitives are not wired into live routing, so no aggregation-filter counter or working `AGGREGATION_ENABLED` switch is advertised yet. Independent DevFee sessions already retain the ASIC subscription agent via `independent_subscription`; pool-specific ban behavior requires a pilot.
