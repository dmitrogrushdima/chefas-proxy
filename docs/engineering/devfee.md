# DevFee: accepted-work accounting and job windows

CHEFAS targets **1% of pool-confirmed difficulty**, not 1% of share count,
wall-clock time, or monetary payouts. The standalone operator release locks
this policy. The source tree also contains a private, restricted test-listener
mode with a manually selected rate; it is not a customer feature.

## What quantization does

The ASIC receives either a customer-pool job or a DevFee-pool job. Each share
is submitted to the upstream session that issued its job. CHEFAS does not
split one proof into fractions or credit a customer's job to another account.
The switch changes work context; keeping TCP open does not mean jobs never
change. Firmware that cannot change context safely uses a reconnect fallback.

For the fixed policy, accepted eligible customer difficulty accrues commission
at `customer_work / 99`. Accepted fee difficulty pays down that balance.
The scheduler admits a fee window only when its accepted-work budget can
fund approximately one fee share. In-flight reservations prevent concurrent
workers from spending the same budget. A funded additional share can keep
the current fee window open instead of immediately switching back.

Whole shares are indivisible. Short windows can deviate from 1%, and a target
is not a promise that every hour or small sample contains exactly 1%.
Pool rejects and unknown outcomes are not counted as accepted work.

## Windows, outages and persistence

The aggregate scheduler defaults to a 60-second configured ceiling and a
5-second cooldown. Groups of up to five workers use at most 15 seconds per
window. These are ceilings and admission constraints, not a fixed periodic
switch schedule. Pool difficulty, worker speed and available quota determine
when actual work moves. See [release settings](../release.md).

Customer work observed while the fee route is unavailable is recorded
separately and does not create retrospective commission debt. The SQLite
journal restores committed work for normal fixed-rate deployments. Unknown
outcomes after disconnects and crashes remain distinguishable from accepted
work; a local journal cannot recover a reply it never observed.

## Private administration versus the operator product

The standalone release fixes the fee account and rate; dashboard or JSON
settings cannot override them. Each farm operator can administer their own
workers and customer routes. That local role does not grant access to the
maintainer's VPS, other farms, or their pool accounts.

The maintainer's separate 3334 listener supports `--fee-test-worker`, requires
an exact worker name, aggregation, an admin token and restricted access. It
allows rates from 1 to 100 percent. Renaming the ASIC worker requires updating
that private binding. Its manually selected rate is separate from normal
fixed-rate fleet accounting. The combined 3333/3334 VPS view is enabled only
for this private listener.

## Verification and limits

Run `python -m pytest -q`, including `test_fee_quantization.py`,
`test_live_devfee_router.py`, `test_fleet_allocator.py` and the journal tests.
CI must run pytest: unittest discovery does not execute the function-based
quantization tests.

Physical qualification must record the exact build, ASIC model and firmware,
pool endpoint, negotiated capabilities, full measurement interval, accepted
difficulty by route, rejects/stale and reconnects. A smooth chart, successful
compilation or a short two-ASIC pilot does not establish universal firmware
compatibility or improved hashrate. See [compatibility](../compatibility.md)
and [release gates](roadmap.md).
