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
