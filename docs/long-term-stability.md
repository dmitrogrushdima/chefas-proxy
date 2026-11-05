# Long-term stability update

These changes add bounded lifecycle rules and synthetic regression coverage. They are not a weeks-long soak test, proof of immunity to pool bans, or certification of commercial pools/ASIC firmware.

## DevFee startup, jitter and retries

`FeeJitter` uses monotonic time and independent OS-backed uniform draws. Default `--devfee-jitter-seconds 180` spreads initial eligibility and subsequent fee windows over 0..180 seconds. Scheduling is per ASIC TCP session: multiple authorized worker names on the same socket share its extranonce and scheduling context.

In normal startup, the ASIC subscribes/authorizes on the user pool immediately. A background supervisor prepares the independent fee connection after its jitter delay; it replays configure/version rolling, subscription and authorized worker credentials. User traffic does not wait for the random delay. `FeeConnectionGate` also spaces fee connection admissions across the entire process (`--devfee-connect-interval .25` by default), including retries. Existing fee connections are reused; switching work is not equivalent to opening a new socket. The pre-existing global fee-owner reservation prevents many sessions from simultaneously owning a fee window.

After a failed fee reader or RPC, the supervisor retries with fresh jitter and at least five seconds of backoff. Cancelling an upstream reader does not cancel its supervisor. Closing an ASIC cancels the supervisor and any queued admission; no detached reconnect loop survives the session. Required-fee outage accounting starts at the first admitted fee attempt after user authorization. The deliberate initial jitter/gate wait is not an outage; subsequent retries never reset the health grace. Actual sustained failure still activates the global block.

Legacy reconnect bootstrap is special: a session already reserved for a fee window must obtain the fee extranonce during subscribe. It skips the additional random delay, while retaining the shared connection gate and authorization deadline. Otherwise every reconnect could restart the delay and never deliver fee work. Hot-capable ASICs continue using set_extranonce, exact difficulty and clean notify.

Accepted-difficulty accounting is unchanged. While a fee channel is not ready, user work is ineligible for later fee debt, including planned startup warmup. Consequently the total fee can be below 1% during warmup/outages; no claim of exactly 1% of payouts is made. `--devfee-jitter-seconds 0 --devfee-connect-interval 0` is available for deterministic laboratory tests; those settings disable startup spreading and should not be copied into a farm rollout.

Admission spacing and randomization reduce synchronized load, but cannot guarantee a pool's rate-limit policy, DNS/TCP scheduling or the behavior of multiple independent proxy processes sharing one public IP. Tune against the pool's documented limits.

## Job lifecycle

The registry was already capped at 128 jobs and 8192 duplicate keys per session; it did not grow without bound. Added `--job-ttl 900` (15 minutes) and `--job-gc-interval 60` provide an age bound in addition to count bounds. Background GC works even if no new notify arrives. Publication also prunes old records; submit performs an O(1) age check for the requested job before forwarding, so expiry is enforced between GC ticks without scanning the whole duplicate cache on every share.

Collection removes jobs, first-share timestamps/worker sets, associated duplicate entries and references to superseded upstream objects. An upstream response already in flight retains its captured issuing context and difficulty until completion; a positive pool response is still accounted even if GC has meanwhile removed the lookup. Session shutdown clears all registries and joins the collector.

The count cap may evict jobs earlier than the TTL; a pool can invalidate jobs earlier too. A valid-looking share older than the configured TTL is rejected locally as an unknown job. Choose TTL according to real job cadence; unusually long-lived work requires a larger TTL. Defaults are bounds on our state, not a guarantee that pools accept every late share.

## TLS routes

Host fields in CLI and routing API now accept `stratum+ssl://HOST[:PORT]` and `stratum+tcp://HOST[:PORT]`, including bracketed IPv6 URLs. A bare host continues to work. A URL port may supply the otherwise omitted port; a separately provided port must match. Credentials in URLs, unrelated schemes, non-root paths, query strings, fragments and invalid ports are rejected. Pool credentials stay in authorize/secret storage rather than URLs. Encrypted route persistence retains the scheme.

All outbound paths use `open_upstream`: active routing, transparent relay, backup connections and management probes. SSL URLs select `ssl.create_default_context()` with mandatory certificate-chain and hostname verification and the normalized host as SNI. There is no automatic plaintext retry for a failed SSL route. `--tls` retains its prior meaning: force verified TLS on all upstreams, including bare or TCP-prefixed hosts. Without that global flag, user/fee/backup transports can differ.

`--tls-ca-file PATH` selects a trusted CA PEM for private-CA pilots; it does not enable TLS on plain routes or disable verification. Default URL contexts reuse the system trust store; load a new process to refresh trust configuration. Invalid CA files fail startup. Verification errors log only the target, session and numeric verification code. Certificate names, account secrets and exception payloads are not dumped by that event.

```sh
python proxy.py --pool-host stratum+ssl://pool.example:443 --active-router --validate-pow
```

For a mixed deployment, use a bare user host/port and an SSL-prefixed DevFee host/port, without the global `--tls` flag. KONTUR renders scheme-bearing endpoints correctly and reports the primary route's effective TLS setting separately from the global force flag. TLS encrypts payloads; it does not make destination IP/port or ordinary SNI invisible, nor guarantee passage through a particular provider's DPI.

References: [Python verified TLS contexts](https://docs.python.org/3.10/library/ssl.html#ssl.create_default_context), [asyncio outbound connections](https://docs.python.org/3.10/library/asyncio-stream.html#asyncio.open_connection).

## Validation: Windows x64 / CPython 3.10, 2026-11-06

- Source regressions: **156 passed, 62 subtests passed**, with the 400-client test excluded from that run. The subsequently added fee RPC-timeout supervisor regression passed separately, as did the unchanged 400-client isolation test. Total source coverage: **158 tests**, executed in parts rather than one monolithic run.
- Job lifecycle tests simulate 2000 publications, enforce the 128-entry count bound, remove old duplicate/timing entries, verify release of superseded channel references, exercise idle background GC, reject an expired submit before GC, and retain accounting for a pool response arriving after collection.
- Jitter tests cover 400 seeded independent timers, bounded eligibility, concurrent admission spacing/cancellation and the distinction between planned warmup and a real fee outage. Live tests use short nonzero jitter for asynchronous warmup/retry behavior; existing exact 990/10 tests explicitly disable jitter to keep their fixture deterministic.
- Source-free native build: **14 .pyd modules** in `dist/kontur-long-term-pilot`. All module import paths and manifest source/artifact hashes were verified. Runtime sources and real credentials are excluded from the artifact.
- Native parser/network/scheduler/TTL/URL/TLS unit tests: **29 passed** after explicitly loading the compiled modules.
- Compiled launcher integration: **27 passed, 2 deselected** with `KONTUR_TEST_ENTRYPOINT` pointing at the absolute generated `launch.py`. The two source-only in-process graceful-shutdown tests were covered by source regressions. Native live coverage includes PoW validation, 1000-share routing, zero-drop switching, failover, nonzero-jitter warmup and retry after a cancelled fee reader, both-TLS and mixed TCP/TLS upstreams, TTL expiry and an accepted in-flight share during GC.
- Frontend JavaScript passed `node --check dashboard/app.js`; URL rendering and effective primary TLS status were integrated without changing the language defaults.

The native artifact requires matching Windows x64 / CPython 3.10. No Linux build, real ASIC/commercial-pool compatibility test, guaranteed DPI bypass, or multi-week uptime claim is established by these results.
