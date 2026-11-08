"""Accepted-difficulty accounting for the live router and the legacy lab model.

The ledger records pool-confirmed work, not merely submissions or share counts.
It asks the router for a fee window when eligible user work exceeds 99 times
fee work. Whole shares and responses already in flight can cross that boundary;
the measured fraction therefore converges to 1% rather than being exactly 1%
after every response. The audit reports that deviation instead of hiding it.
"""

from decimal import Decimal, InvalidOperation
from fractions import Fraction
import math
import asyncio
import random
import time
from dataclasses import dataclass
from collections import deque
import string
import heapq


def independent_subscription(params):
    """Retain the original agent/version; never resume another pool's session."""
    return list(params[:1])


class FeeJitter:
    """Per-session monotonic eligibility; accepted-work accounting is unchanged."""

    def __init__(self, maximum: float = 180, *, clock=None, uniform=None):
        if not math.isfinite(maximum) or not 0 <= maximum <= 3600:
            raise ValueError('jitter must be 0..3600 seconds')
        self.maximum = float(maximum)
        self.clock = clock or time.monotonic
        self.uniform = uniform or random.SystemRandom().uniform
        self.defer()

    def defer(self):
        self.eligible_at = self.clock() + self.uniform(0, self.maximum)

    def remaining(self):
        return max(0, self.eligible_at - self.clock())


class FeeConnectionGate:
    """Process-wide spacing of connection attempts, including retries."""

    def __init__(self, interval: float = .25):
        if not math.isfinite(interval) or not 0 <= interval <= 60:
            raise ValueError('connection interval must be 0..60 seconds')
        self.interval = float(interval)
        self.lock = asyncio.Lock()
        self.last_attempt = None

    async def wait(self, delay=0):
        await asyncio.sleep(delay)
        async with self.lock:
            if self.last_attempt is not None:
                deadline = self.last_attempt + self.interval
                while (remaining := deadline - time.monotonic()) > 0:
                    await asyncio.sleep(remaining)
            self.last_attempt = time.monotonic()
            return self.last_attempt


def _difficulty(value, *, allow_zero=False):
    """Convert wire decimals without binary float accumulation or huge exponents."""
    if isinstance(value, bool):
        raise ValueError("difficulty must be a finite positive number")
    try:
        if isinstance(value, Fraction):
            result = value
        elif isinstance(value, int):
            result = Fraction(value)
        elif isinstance(value, (str, Decimal, float)):
            if isinstance(value, str) and len(value) > 256:
                raise ValueError("difficulty is too long")
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError("difficulty must be finite")
            decimal = value if isinstance(value, Decimal) else Decimal(str(value))
            if not decimal.is_finite() or abs(decimal.as_tuple().exponent) > 1024:
                raise ValueError("difficulty must be finite and bounded")
            result = Fraction(decimal)
        else:
            raise ValueError("unsupported difficulty type")
    except (InvalidOperation, OverflowError, TypeError, ZeroDivisionError) as exc:
        raise ValueError("invalid difficulty") from exc
    if result < 0 or (not allow_zero and result == 0):
        raise ValueError("difficulty must be positive")
    if result.numerator.bit_length() > 4096 or result.denominator.bit_length() > 4096:
        raise ValueError("difficulty is too large or too small")
    return result


def _exact(value):
    """Use finite decimals when possible; otherwise retain an exact fraction."""
    numerator, denominator = value.numerator, value.denominator
    twos = fives = 0
    while denominator % 2 == 0:
        denominator //= 2
        twos += 1
    while denominator % 5 == 0:
        denominator //= 5
        fives += 1
    if denominator != 1:
        return str(value)
    places = max(twos, fives)
    scaled = numerator * 2 ** (places - twos) * 5 ** (places - fives)
    if not places:
        return str(scaled)
    sign = "-" if scaled < 0 else ""
    digits = str(abs(scaled)).zfill(places + 1)
    return sign + digits[:-places] + "." + digits[-places:]


class AcceptedWorkLedger:
    """Fixed 1/100 target with exact work and no fee debt for pool outages.

    ``difficulty`` must come from the job attached to the submitted share,
    before any vardiff or upstream transition. Only an unambiguous positive
    pool response is recorded with ``accepted=True``. Late fee responses are
    always counted, even if their upstream has become unavailable meanwhile.

    ``min_user_work`` batches windows in difficulty units. Socket reset
    cooldowns belong to the router, which knows the miner's capabilities.
    """

    def __init__(self, min_user_work=0):
        self.min_user_work = _difficulty(min_user_work, allow_zero=True)
        self.user_work = Fraction(0)
        self.eligible_user_work = Fraction(0)
        self.fee_work = Fraction(0)
        self.user_shares = 0
        self.fee_shares = 0
        self.max_fee_share_work = Fraction(0)
        self._user_since_window = Fraction(0)
        self._window_active = False

    def record(self, target, difficulty, accepted=True, *, fee_available=True):
        if target not in ("user", "fee"):
            raise ValueError("target must be user or fee")
        if not isinstance(accepted, bool) or not isinstance(fee_available, bool):
            raise ValueError("accepted and fee_available must be boolean")
        work = _difficulty(difficulty)
        if not accepted:
            return
        if target == "user":
            self.user_work += work
            self.user_shares += 1
            if fee_available:
                self.eligible_user_work += work
                self._user_since_window += work
        else:
            self.fee_work += work
            self.fee_shares += 1
            self.max_fee_share_work = max(self.max_fee_share_work, work)
            self._window_active = True
            if self.fee_work * 99 >= self.eligible_user_work:
                self._window_active = False
                self._user_since_window = Fraction(0)

    def should_fee(self, *, fee_available=True):
        if not isinstance(fee_available, bool):
            raise ValueError("fee_available must be boolean")
        return (fee_available and self.fee_work * 99 < self.eligible_user_work
                and (self._window_active or self._user_since_window >= self.min_user_work))

    def snapshot(self):
        total = self.user_work + self.fee_work
        eligible_total = self.eligible_user_work + self.fee_work
        # This is an observed deviation, not a promise about future in-flight
        # shares. The router can have several already accepted responses pending.
        excess = max(Fraction(0), self.fee_work - self.eligible_user_work / 99)
        return {
            "user_accepted_difficulty": _exact(self.user_work),
            "fee_accepted_difficulty": _exact(self.fee_work),
            "outstanding_fee_difficulty": _exact(max(Fraction(0), self.eligible_user_work / 99 - self.fee_work)),
            "eligible_user_difficulty": _exact(self.eligible_user_work),
            "outage_user_difficulty": _exact(self.user_work - self.eligible_user_work),
            "user_accepted_shares": self.user_shares,
            "fee_accepted_shares": self.fee_shares,
            "target_percentage": 1.0,
            "user_percentage": float(100 * self.user_work / total) if total else None,
            "fee_percentage": float(100 * self.fee_work / total) if total else None,
            "eligible_fee_percentage": float(100 * self.fee_work / eligible_total) if eligible_total else None,
            "fee_excess_difficulty": _exact(excess),
            "max_fee_share_difficulty": _exact(self.max_fee_share_work),
            "rounding_bound_difficulty": _exact(max(self.max_fee_share_work, excess)),
            "rounding_bound_basis": "observed fee share quantum or current excess; excludes future in-flight responses",
        }


@dataclass(frozen=True)
class PendingFeeWork:
    """Reservation for one submitted proof; never an accepted-work credit."""

    worker_id: int | str
    difficulty: Fraction


class FairnessAllocator:
    """Allocate concurrent fee windows against one exact accepted-work budget.

    A grant reserves one *expected* fee share at that upstream's job difficulty.
    Thus thousands of ASICs cannot each spend the same outstanding fee debt.
    Allocation is proportional to difficulty units, not a count of workers or
    shares. The last indivisible grant may cross the debt by one share quantum;
    subsequent grants are denied until accepted user work funds them again.

    The event-loop owner calls these synchronous methods without intervening
    awaits. ``begin_submit`` transfers an unused grant into a pending reservation
    and also tracks late proofs from old jobs, even without an active grant.
    ``release`` never discards pending work. Record a positive pool response in
    the ledger *before* ``finish_submit``. Rejected or ambiguous outcomes are not
    income; an unknown outcome releases its reservation and is counted explicitly
    rather than retried, because Stratum provides no definitive reconciliation.

    This bounds allocation, not the stochastic number of proofs already being
    computed or in transit. Valid late proofs must still be forwarded and counted.
    No algorithm can promise exactly 1% after each indivisible share, or infer a
    pool's acceptance after a lost response. This class does not open sockets or
    merge ASIC nonce spaces; those are responsibilities of the live router.
    """

    def __init__(self, ledger: AcceptedWorkLedger, max_active: int = 128,
                 max_pending: int = 10000) -> None:
        if not isinstance(ledger, AcceptedWorkLedger):
            raise ValueError('allocator requires an accepted-work ledger')
        for name, value in (('max_active', max_active), ('max_pending', max_pending)):
            if type(value) is not int or not 1 <= value <= 10000:
                raise ValueError(name + ' must be 1..10000')
        self.ledger = ledger
        self.max_active = max_active
        self.max_pending = max_pending
        self._grants: dict[int | str, Fraction] = {}
        self._pending: dict[int, PendingFeeWork] = {}
        self._pending_workers: dict[int | str, int] = {}
        self._reserved = Fraction(0)
        self._sequence = 0
        self.accepted_submissions = 0
        self.rejected_submissions = 0
        self.unknown_submissions = 0
        self.unknown_difficulty = Fraction(0)

    @staticmethod
    def _worker(worker_id: int | str) -> None:
        if ((type(worker_id) is int and worker_id >= 0 and worker_id.bit_length() <= 256)
                or (isinstance(worker_id, str) and 0 < len(worker_id) <= 256)):
            return
        raise ValueError('invalid allocator worker identity')

    @property
    def outstanding(self) -> Fraction:
        """Actual accepted-work debt, which may be negative after late proofs."""
        return self.ledger.eligible_user_work / 99 - self.ledger.fee_work

    @property
    def reserved(self) -> Fraction:
        return self._reserved

    def active(self, worker_id: int | str) -> bool:
        self._worker(worker_id)
        return worker_id in self._grants

    def should_continue(self, worker_id: int | str) -> bool:
        """Revalidate an idle miner's grant after other pool responses settle."""
        self._worker(worker_id)
        own = self._grants.get(worker_id)
        return (own is not None and self.ledger.should_fee() and
                self.outstanding - (self._reserved - own) > 0)

    def acquire(self, worker_id: int | str, difficulty) -> bool:
        """Reserve one share; a duplicate acquire cannot reserve it twice.

        Reacquiring an existing grant revalidates and, if needed, updates its
        difficulty for a new job. A worker with an outstanding RPC cannot acquire
        another speculative grant. The router should skip periodic reconciliation
        while that submit is pending, then reconcile after its response settles.
        """
        self._worker(worker_id)
        work = _difficulty(difficulty)
        own = self._grants.get(worker_id, Fraction(0))
        budget = self.outstanding - (self._reserved - own)
        if (not self.ledger.should_fee() or budget <= 0 or
                worker_id in self._pending_workers or
                (worker_id not in self._grants and len(self._grants) >= self.max_active)):
            self.release(worker_id)
            return False
        self._grants[worker_id] = work
        self._reserved += work - own
        return True

    def release(self, worker_id: int | str) -> None:
        """Release unsubmitted work only, including on disconnect or failover."""
        self._worker(worker_id)
        self._reserved -= self._grants.pop(worker_id, Fraction(0))

    def begin_submit(self, worker_id: int | str, difficulty) -> int:
        """Reserve exact work for a live or late proof before sending it upstream.

        Admission limits may reject a new reservation before any bytes are sent;
        the router must handle that as a local capacity failure, not an accepted
        or rejected pool share. Old proofs do not need a new grant and remain
        routable even when the accepted-work budget has already been repaid.
        """
        self._worker(worker_id)
        work = _difficulty(difficulty)
        if len(self._pending) >= self.max_pending:
            raise ValueError('fee pending reservation capacity exhausted')
        self.release(worker_id)
        self._sequence += 1
        self._pending[self._sequence] = PendingFeeWork(worker_id, work)
        self._pending_workers[worker_id] = self._pending_workers.get(worker_id, 0) + 1
        self._reserved += work
        return self._sequence

    def finish_submit(self, token: int, outcome: str = 'accepted') -> None:
        """Release an RPC reservation exactly once; never mutate the ledger."""
        if type(token) is not int or token not in self._pending:
            raise ValueError('unknown or already settled fee reservation')
        if outcome not in ('accepted', 'rejected', 'unknown'):
            raise ValueError('invalid fee submission outcome')
        pending = self._pending.pop(token)
        self._reserved -= pending.difficulty
        count = self._pending_workers[pending.worker_id] - 1
        if count:
            self._pending_workers[pending.worker_id] = count
        else:
            del self._pending_workers[pending.worker_id]
        counter = outcome + '_submissions'
        setattr(self, counter, getattr(self, counter) + 1)
        if outcome == 'unknown':
            self.unknown_difficulty += pending.difficulty

    def snapshot(self) -> dict:
        return {
            'active_grants': len(self._grants),
            'pending_submissions': len(self._pending),
            'reserved_difficulty': _exact(self._reserved),
            'available_difficulty': _exact(max(Fraction(0), self.outstanding - self._reserved)),
            'accepted_submissions': self.accepted_submissions,
            'rejected_submissions': self.rejected_submissions,
            'unknown_submissions': self.unknown_submissions,
            'unknown_difficulty': _exact(self.unknown_difficulty),
            'max_active': self.max_active,
            'max_pending': self.max_pending,
        }


class FeeWindows:
    """Route leases are independent of outstanding submit RPC reservations."""

    def __init__(self, allocator, ttl, cooldown, overdraft, clock=time.monotonic):
        self.allocator = allocator
        self.ttl, self.cooldown = ttl, cooldown
        self.overdraft = _difficulty(overdraft, allow_zero=True)
        self.clock = clock
        self.groups = {}
        self.members = {}
        self.leases = {}
        self.ready = {}
        self.cooling = {}
        self.samples = {}

    def join(self, worker, group):
        if self.members.get(worker) == group:
            return
        self.leave(worker)
        self.members[worker] = group
        self.groups.setdefault(group, {})[worker] = None

    def leave(self, worker):
        self.end(worker)
        group = self.members.pop(worker, None)
        if group is not None:
            members = self.groups[group]
            del members[worker]
            if not members:
                del self.groups[group]
        self.ready.pop(worker, None)
        self.cooling.pop(worker, None)
        self.samples.pop(worker, None)

    def observe(self, worker, difficulty):
        if worker not in self.members:
            return
        bucket = int(self.clock())
        samples = self.samples.setdefault(worker, deque())
        self.weight(worker)
        work = _difficulty(difficulty)
        if samples and samples[-1][0] == bucket:
            samples[-1] = (bucket, samples[-1][1] + work)
        else:
            samples.append((bucket, work))

    def weight(self, worker):
        samples = self.samples.get(worker)
        if not samples:
            return Fraction(0)
        cutoff = int(self.clock()) - 300
        while samples and samples[0][0] <= cutoff:
            samples.popleft()
        return sum((work for _, work in samples), Fraction(0))

    def expired(self, worker):
        expiry = self.leases.get(worker)
        return expiry is not None and self.clock() >= expiry

    def eligible(self, worker, work, budget):
        return (self.clock() >= self.cooling.get(worker, 0)
                and budget >= work * (1 - self.overdraft)
                and budget > 0)

    def acquire(self, worker, difficulty):
        if difficulty is None:
            self.ready.pop(worker, None)
            return False
        work = _difficulty(difficulty)
        self.ready[worker] = work
        group = self.groups[self.members[worker]]
        if not self.allocator.ledger.should_fee():
            return False
        budget = self.allocator.outstanding - self.allocator.reserved
        active = worker in self.leases
        if not active and any(member in self.leases for member in group):
            return False
        # A single worker is the admission limit. On large groups also enforce
        # the weighted limit, rejecting a candidate whose weight is unknown.
        weights = {member: self.weight(member) for member in group} if len(group) > 50 else {}
        cap = sum(weights.values()) * Fraction(3, 100) if weights else None
        if active:
            budget += self.allocator._grants.get(worker, 0)
            return (not self.expired(worker) and self.eligible(worker, work, budget)
                    and (cap is None or 0 < weights[worker] <= cap)
                    and self.allocator.acquire(worker, work))
        for member in group:
            candidate = self.ready.get(member)
            if candidate is None or not self.eligible(member, candidate, budget):
                continue
            if weights and not 0 < weights[member] <= cap:
                continue
            if member != worker or not self.allocator.acquire(worker, work):
                return False
            del group[worker]
            group[worker] = None
            duration = min(self.ttl, 15) if len(group) <= 5 else self.ttl
            self.leases[worker] = self.clock() + duration
            return True
        return False

    def end(self, worker):
        if self.leases.pop(worker, None) is not None:
            self.cooling[worker] = self.clock() + self.cooldown
        self.allocator.release(worker)


class StableFeeSlots:
    """Bounded, reusable slot names for concurrently live fee sessions.

    Session identities may grow across weeks of uptime; the pool worker suffix
    remains within ``1..limit``. A slot can be reused only after the owner has
    closed *all* its old fee channels, so the router releases it at the end of
    cleanup, not on a transient pool outage or on leaving a fee window.
    """

    def __init__(self, limit: int = 10000) -> None:
        if type(limit) is not int or not 1 <= limit <= 10000:
            raise ValueError('fee slot limit must be 1..10000')
        self.limit = limit
        self._owners: dict[int | str, int] = {}
        self._free: list[int] = []
        self._next = 1

    def obtain(self, session_id: int | str) -> int:
        FairnessAllocator._worker(session_id)
        if session_id in self._owners:
            return self._owners[session_id]
        if self._free:
            slot = heapq.heappop(self._free)
        elif self._next <= self.limit:
            slot = self._next
            self._next += 1
        else:
            raise ValueError('fee worker slot capacity exhausted')
        self._owners[session_id] = slot
        return slot

    def release(self, session_id: int | str) -> None:
        FairnessAllocator._worker(session_id)
        slot = self._owners.pop(session_id, None)
        if slot is not None:
            heapq.heappush(self._free, slot)


def stable_fee_worker(worker: str, slot: int, template: str | None = None) -> str:
    """Only an explicit operator template may alter an upstream account name.

    For example, ``'{worker}_{slot}'`` expands ``wallet.worker`` to
    ``wallet.worker_7``. Pool account and wallet syntaxes differ; the default
    preserves the operator's exact authorization string instead of guessing.
    Slots identify sessions, not hardware models or an assurance of invisibility.
    """
    if not isinstance(worker, str) or not worker or len(worker) > 128 or any(ord(c) < 32 for c in worker):
        raise ValueError('invalid fee worker')
    if type(slot) is not int or not 1 <= slot <= 10000:
        raise ValueError('fee worker slot must be 1..10000')
    if template is None:
        return worker
    if not isinstance(template, str) or not template or len(template) > 256:
        raise ValueError('invalid fee worker template')
    try:
        fields = list(string.Formatter().parse(template))
        for _, field, spec, conversion in fields:
            if field is not None and (field not in ('worker', 'slot') or spec or conversion):
                raise ValueError('template supports only {worker} and {slot}')
        if not any(field == 'worker' for _, field, _, _ in fields):
            raise ValueError('template must retain {worker}')
        result = template.format(worker=worker, slot=slot)
    except (KeyError, IndexError, AttributeError) as exc:
        raise ValueError('invalid fee worker template') from exc
    if not result or len(result) > 128 or any(ord(c) < 32 for c in result):
        raise ValueError('expanded fee worker exceeds limits')
    return result


class FeeSimulation:
    """Model a 1% target against eligible accepted work without outage debt."""

    def __init__(self):
        self.user_work = 0
        self.eligible_user_work = 0
        self.fee_work = 0

    def select(self, fee_available=True):
        if not isinstance(fee_available, bool):
            raise ValueError("fee_available must be boolean")
        if fee_available and self.fee_work * 99 < self.eligible_user_work:
            return "fee"
        return "user"

    def record(self, route, work=1, fee_available=True):
        if route not in ("user", "fee"):
            raise ValueError("invalid route")
        if isinstance(work, bool) or not isinstance(work, int) or work <= 0:
            raise ValueError("work must be a positive integer")
        if not isinstance(fee_available, bool):
            raise ValueError("fee_available must be boolean")
        if route == "user":
            self.user_work += work
            if fee_available:
                self.eligible_user_work += work
        else:
            self.fee_work += work

class FeeHealth:
    """Monotonic grace policy; not a tamper-proof license mechanism.

    A healthy socket alone cannot clear a sustained rejected-share incident.
    Silence (no submitted fee work) is not treated as a rejection.
    """

    def __init__(self, grace_seconds: float, clock=None) -> None:
        import time
        if not math.isfinite(grace_seconds) or grace_seconds <= 0:
            raise ValueError('grace must be positive and finite')
        self.grace_seconds: float = float(grace_seconds)
        self.clock = clock or time.monotonic
        self.unavailable_since: float | None = self.clock()
        self.rejected_since: float | None = None

    def observe(self, available: bool) -> None:
        if available:
            self.unavailable_since = None
        elif self.unavailable_since is None:
            self.unavailable_since = self.clock()

    def share_result(self, accepted: bool) -> None:
        if accepted:
            self.rejected_since = None
        elif self.rejected_since is None:
            self.rejected_since = self.clock()

    def expired(self) -> bool:
        now = self.clock()
        return any(start is not None and now - start >= self.grace_seconds
                   for start in (self.unavailable_since, self.rejected_since))
