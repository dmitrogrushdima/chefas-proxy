# Network hardening for the pilot

Implemented in `stratum_codec.py`, `stratum_session.py`, `router.py` and `upstream_manager.py`. Synthetic tests establish protocol behavior; they do not certify ASIC firmware or commercial pools.

Follow-up: [long-term stability](long-term-stability.md) adds fee connection jitter/admission, job TTL and URL-selected TLS. Its planned startup delay precedes required-fee outage accounting.

## Framing and scanner rejection

Active routing uses a bounded streaming UTF-8 JSON-object decoder in both directions. Objects may be split across TCP reads, glued without newlines, or end exactly at EOF. The router still emits conventional newline-delimited JSON. A partial object at EOF is rejected, never executed. Strings, escaped delimiters, multibyte UTF-8 and Unicode escape boundaries are tracked across reads.

The decoder scans bytes once and calls JSON decoding once per balanced object. It rejects invalid UTF-8/escapes, duplicate keys, lone surrogates, nonfinite or excessive numbers, more than 32 nesting levels, invalid method/request envelopes and unbounded whitespace. After an error it closes the connection; it does not attempt to resynchronize uncertain job boundaries. Active-mode miner methods are explicitly listed in `stratum_codec.MINER_METHODS`; unknown firmware extensions require review and tests. Pool-initiated bidirectional RPC and arbitrary firmware protocols are not certified.

`--max-miner-message 4096` limits each ASIC JSON object, configurable between 512 and 65536 bytes. It does **not** limit a TCP packet or a burst of multiple valid objects. Upstream objects retain a separate 65536-byte budget because coinbase/merkle jobs may exceed 4 KiB. Reader chunks are 1024 bytes and decoded-message queues are limited by each chunk. The frame accumulator is bounded; asyncio transport buffers use flow control and OS socket buffers have separate limits, so 4 KiB is not a claim about total connection memory.

Before opening any upstream socket, the active router requires one valid miner request. HTTP/TLS scanners, binary garbage and oversized first objects are aborted quietly. Incomplete first objects are covered by the handshake deadline. Counters `protocol_drops` and `handshake_timeouts` expose these events without packet dumps or per-scanner INFO records. Capacity and IP filters remain in front of parsing. This is application hardening, not upstream volumetric DDoS protection.

## Miner liveness and resource reclamation

`SO_KEEPALIVE` is enabled on accepted ASIC sockets. Where supported, kernel probes are tuned with idle 60 seconds, interval 15 seconds and count 3. Unsupported OS tuning is tolerated; `keepalive_unavailable` counts failure to enable keepalive itself. The application timer is independent of kernel support.

`--handshake-timeout 30` bounds the entire active-mode authorization phase, including upstream setup. Fragment delivery and unauthenticated pings never renew it. `--miner-timeout 600` bounds authenticated silence independently of traffic from pools. It is renewed by a structurally valid, known, nonduplicate submit (and valid PoW when the gatekeeper is enabled), or a local `mining.ping` with empty parameters. That ping is an optional KONTUR extension, not a claim that Stratum ASICs universally implement it. A pool rejection can still prove inbound liveness; merely sending malformed/unknown/duplicate shares cannot.

The Reaper checks deadlines at most every 250 ms plus event-loop scheduling delay. It aborts the ASIC transport and upstream transports, cancels the session owner even if it awaits an RPC, then joins reader/tasks and releases session/worker counters. `zombie_reaped` records expiry. Set the miner deadline after measuring actual share cadence: high difficulty or low hashrate can produce long legitimate gaps. `--miner-timeout 0` disables this timer, not the authorization deadline. No notify-to-submit delay is mislabeled as network RTT.

Transparent mode remains byte-preserving: it does not apply JSON scanner checks, and its miner timer counts inbound bytes rather than validated submissions. Its existing bounded half-close response grace remains separate. Active-mode full TCP half-close is still not implemented.

Required-DevFee health monitoring starts only after successful user authorization, preventing an unauthenticated socket from triggering the process-wide fee outage latch. An already latched block remains enforced immediately on new connections.

## Vardiff synchronization

Difficulty is not numerically interpolated. Substituting a lower or intermediate threshold would create shares the issuing pool may reject and distort accepted-work accounting. ASIC reboot behavior under large target changes requires real-device testing.

Each mapped job captures issuing connection, extranonce, version mask and difficulty. A pending `mining.set_difficulty` applies to the next notify, not an old cached job. Atomic activation emits `set_extranonce -> set_difficulty(cached job threshold) -> notify(clean_jobs=True)` under the ASIC writer lock. Normal publication also sends that job's exact difficulty before notify. A queued publication or difficulty notification rechecks the active upstream **after acquiring** that same lock, preventing an old pool's job from following a new pool's extranonce. Old job mappings remain bound to their issuing connection within the bounded history; the pool can still reject work it has invalidated.

Tests cover coalescing/fragmentation, Unicode and malformed input, exact decimal preservation, silent scanner rejection before upstream setup, partial-handshake expiry, pool-notification-independent reaping, authenticated ping liveness, fee-watchdog isolation, deterministic writer-lock races, and existing PoW-validated live switching/failover.

## Validation: Windows x64 / CPython 3.10, 2026-11-05

- Source suite excluding the 400-client test: **140 passed, 48 subtests passed** (`python -m pytest -q --tb=short -k "not many_clients_keep_traffic_separate"`).
- Unmodified 400-client isolation test, run separately: **1 passed**, with its existing 20-second read timeout (`python -m pytest tests/test_proxy.py::RelayTests::test_many_clients_keep_traffic_separate -q --tb=short`). An earlier full run exceeded that timeout; transparent-mode Reaper waiting was subsequently folded into the existing pump wait to avoid an additional long-lived task per connection. The final source results above were obtained in two runs, not a single monolithic run. This is not a production capacity benchmark.
- Native build: **14 .pyd modules** in `dist/kontur-network-pilot`; all runtime imports were verified to resolve to that directory, and every manifest file/source hash matched.
- Native codec, writer-race, Keepalive and upstream tests: **14 passed** using unittest after preloading the verified compiled modules.
- Actual compiled launcher integration: **21 passed, 2 deselected**. `KONTUR_TEST_ENTRYPOINT` pointed at the absolute path of `dist/kontur-network-pilot/launch.py`; `tests/test_live_devfee_router.py` ran with `-k "not graceful_proxy_shutdown and not sigterm_closes_live"`. The two excluded tests call the source `run()` function in-process and were covered by the source suite. This native run includes 1000 cryptographically validated accepted shares (990 user / 10 fee), hot switching, old-job routing, failover, parser drops and Reaper resource reclamation.

The artifact targets Windows CPython 3.10 x64. Linux native builds, long-running lossy-network trials and real ASIC/pool interoperability remain pilot work.
