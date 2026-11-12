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

## One-listener result

The private 3333 trial covered 13:41:28–14:41:43 UTC on 2026-11-12,
after startup settling: 3,615.34 seconds. The 120-second cooldown was kept
only on this listener; public defaults and private 3334 were not changed.

Pool-accepted primary work averaged 234.03 TH/s, fee work 2.40 TH/s,
total 236.44 TH/s, versus the earlier primary/total hour of 232.06/234.40.
There were 30 fee visits (29.87/hour), versus 76 in the baseline hour.
The median entry interval was 121.23 s and median fee visit 1.24 s.
The cumulative eligible-work fee percentage at the interval end was 0.999983%.

There were no new pool rejects in the measured hour and no miner reconnects.
One startup stale preceded the first fee visit. Two early local rejections
remained unchanged at final control; there were no unknown worker outcomes.
The trial watchdog was explicitly disarmed after review, leaving the ASIC
working with the selected configuration and no deferred rollback.

Return-to-primary delay averaged 11.36 s over 30 new samples, versus 8.72 s
for 319 ordinary primary intervals. This does not establish a cheaper individual
switch; the clear change is fewer context switches. The different observation
times and pool difficulty distributions prevent attributing the small hashrate
difference to the cooldown. Zero observed rejects in both measured intervals
does not demonstrate a lower rare-stale probability or universal compatibility.

Both main-route and active-fee SIGTERM regressions passed against an isolated
copy of the installed tree. GitHub CI run 43233949672 passed Ubuntu, Windows
and the dedicated Python 3.12 shutdown job for source commit `adac2be`.

## Later decline and conservative rollback

The successful hour did not establish sustained improvement. At 15:11 UTC,
the same process showed about 212 TH/s accepted over one hour and 192 TH/s
over 15 minutes. No new pool rejects, reconnects, transport errors or journal
errors accompanied the decline. The next half-hour after the original trial
averaged 201.33 TH/s primary work. Return delay was 10.53 s versus 9.91 s for
ordinary primary gaps; that observed difference does not explain the whole dip.
The 15-minute estimate recovered to about 225 TH/s before any production change.

Two confirmed observability defects were fixed separately from routing:

- `DeviceWork` discarded the boundary minute but divided by the full requested
  period. A constant-rate regression reproduced a 6.67% low 15-minute estimate
  at a boundary. The corrected divisor is the actual retained bucket coverage,
  also returned as `measured_seconds`; `requested_seconds` preserves the nominal
  horizon. A nominal 15-minute estimate thus covers 14–15 minutes, not an exact
  second-resolution window. The second-resolution `WorkAudit` was unaffected.
- Aggregate local refusals lacked cause logs and worker counters. They now
  distinguish duplicate, unknown/stale context, low target and malformed proof
  without logging raw submitted values. Protocol diagnostics expose the local
  and upstream difficulty and the miner-reported agent.

The problem half-hour contained 158 primary accepts at difficulty 524288.
For constant 230 TH/s and independent proofs at that difficulty, expected
counts are about 92 per 15 minutes or 368 per hour. A normal approximation to
Poisson variation gives roughly 183–277 TH/s over 15 minutes and 206–254 TH/s
over one hour. These are conditional estimates, not evidence that the physical
ASIC maintained constant power. ASIC firmware telemetry was unavailable.

At 15:31:53 UTC, private 3333 was conservatively returned to a 45-second cooldown
with the validated statistics/diagnostic patches. The service restart took
0.123 s; no SIGKILL was needed. The 1% accepted-difficulty policy and shutdown
fix were preserved, and 3334 was unchanged. This rollback removes an unproven
configuration; it does not establish that the 120-second cooldown caused the dip.
