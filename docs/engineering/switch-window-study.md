# DevFee switch-window study — 2026-11-12

Purpose: reduce avoidable job-context changes while preserving the fixed 1%
target measured in pool-accepted difficulty. This does not increase ASIC chip
hashrate and is not cross-account forwarding of a customer's completed proof.

Scope: private 3333 listener only. Leave private 3334 and public release assets
unchanged. Do not create a release or tag for this investigation.

## Shutdown prerequisite

The old shutdown path entered `async with server, dashboard` and awaited server
closure before closing miner/SSE transports. Python 3.12 waits for accepted
transports in `Server.wait_closed()`, creating a circular wait. Reproduced with
a real proxy process, synthetic authenticated pool, valid submitted proof,
connected miner and authenticated SSE reader. SIGTERM exceeded eight seconds.

Fix: close admission, reconnect/close miners, close SSE streams and upstreams,
then await listener closure and persist history/journal. The regression test
requires exit code zero and `proxy_stopped`, with clients still open at SIGTERM.
Patched repository passed in 0.304 s on Linux Python 3.12; an isolated copy of
the actual installed 3333 tree with only the shutdown block patched passed in
0.408 s. These are whole-test durations, not ASIC reconnection times.

## Baseline and candidate

Baseline 2026-11-12 15:19–16:19 MSK: 76 DevFee entries, median entry interval
45.426 s, median fee visit 0.386 s, total fee time 35.456 s. No new pool rejects
between the first and last minute summaries. Before the previous attempt,
one-hour accepted hashrate was 234.40 TH/s total and 232.06 TH/s primary.

Cumulative mean return-to-primary accepted-share delay was 9.60 s versus
7.28 s for ordinary primary intervals. This is observational and includes share
arrival randomness; the 2.32 s difference is not proven lost hashing time.

Candidate: cooldown 120 s instead of 45 s; preserve fee percentage, accepted-work
accounting, window ceiling, routing, validation and pool difficulty settings.
Expected maximum visit frequency drops from roughly 80 to 30 per hour. This
is a lower bound between visits, not a rigid fee timetable.

## Evaluation

Record exact interval, process/session identity, accepted difficulty per route,
eligible and outage work, new rejects/stale, ordinary gaps and post-fee delays.
Use counter deltas, not lifetime means. Exclude restart/initial pool vardiff
settling from comparisons. A short sample can check stability, but cannot
establish improved one-hour hashrate or a lower rare-stale probability.

Restore the saved configuration on loss of accepted work, repeated disconnects
or a marked reject increase. Maintain a bounded rollback watchdog during the
trial and explicitly disarm it when the final configuration is decided.
Detailed private observations are kept outside tracked documentation under
`dist/switch-review-20261112/`; they must not be published as promotional data.
