"""Experimental offline accounting model. Not connected to the mining proxy.

This module does not switch pools, authorize workers, or handle a mining job.
Never use it to route real hashrate. The work units must be positive integers
in a consistent scale, based on shares accepted by their respective pools.
"""


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
