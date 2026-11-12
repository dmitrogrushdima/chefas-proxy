# Fee-funded clean-job loss protection

This is an opt-in, single-worker qualification feature for aggregate routing.
It is not enabled in operator packages and is not a zero-reject guarantee.
Private adjustable-fee listeners are excluded. No release asset is replaced by
this source change.

## What changes real work

Known duplicate, malformed, below-target and invalidated assignments are already
filtered locally. A pool may still invalidate a valid assignment after submit;
local filtering cannot predict that race. A rejected proof is never rerouted to
a fee account, counted as accepted, or removed from the reject counters.

For a unique PoW-checked primary proof, protection requires both a definite pool
error 21 (stale) and a clean-job generation increase during that submit. That
share's **job difficulty**, not the unusually lucky hash's actual difficulty,
is deducted from future fee entitlement. Generic errors, timeouts, low-target
proofs, duplicates and stale submissions without that observed race receive no
credit. Fee-route rejects receive no primary loss credit.

Let U be eligible accepted primary difficulty, F accepted fee difficulty and L
protected lost difficulty. The remaining budget is `U/99 - F - L`. The scheduler
therefore issues fewer fee jobs and the same worker does more primary work.
For example, after U=198, F=1, L=1 no second fee share is funded. After U=297,
F=2, L=1 the customer has 99% of the total accepted-plus-lost work. With losses
and fee difficulty differing, exact fractions and normal indivisible-share
rounding apply. This trades developer income for primary work; it does not
create hashrate, refund coins already paid, or recover all hardware/network
losses. Heavy losses can consume the entire fee budget. The protection can carry
an unfunded reduction forward; it cannot promise immediate full compensation.

## Qualification scope

Start only with an existing confirmed ASIC identity, using
`--fee-loss-protection --fee-loss-worker ACCOUNT.WORKER` in addition to the
existing aggregate active-router arguments. Multiple connections of that same
worker remain allowed. Other worker identities are refused before upstream
authorization, so a different customer cannot consume the compensation.
Tenant profiles, backup pools and private manual-rate controls are excluded
from this initial mode. Default routing without these flags retains its policy.

The SQLite journal stores the reject and reduction in one transaction and binds
the ledger to the primary pool/worker scope. Startup refuses a different scope
instead of silently transferring credits. Keep the scoped identity and original
journal when restarting. Turning off *new* loss protection while retaining
`--fee-loss-worker` still honors existing reductions; do not downgrade to an
older binary that does not understand the loss table. A rollback must keep the
new accounting or explicitly reconcile any awarded credit first. Keep the
original scoped journal if changing the worker or pool; do not delete evidence
to bypass the binding.

Audit JSON exposes `loss_protection_enabled`, `protected_loss_difficulty`,
`protected_loss_shares`, `fee_reduction_difficulty`, `unfunded_loss_difficulty`
and `net_fee_target_difficulty`, alongside unmodified real U/F and reject counts.
The reduction is a scheduling budget, not proof of additional pool-accepted
work already delivered. Actual `fee_percentage` can fall below the nominal 1%.
There is no panel control to change the nominal fee rate.

## Evidence

The live TCP pool fixture independently checks header PoW and inserts a clean
update before rejecting one valid proof. With protection disabled it accepts
198 primary shares and two fee shares; with protection enabled it accepts 198
primary shares and only one fee share, then requires 99 more primary shares to
fund the second fee share. The original stale remains visible, its replay is
filtered before the pool, and another worker is denied. Additional cases check
non-stale errors, missing clean updates, persistent credits, scope mismatch and
transaction rollback. These tests establish actual routing/accounting changes
on the stand, not ViaBTC behavior or a guarantee of physical hashrate recovery.
