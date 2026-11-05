from decimal import Decimal
from fractions import Fraction
import json
import unittest

from devfee_scheduler import AcceptedWorkLedger


class AcceptedWorkLedgerTests(unittest.TestCase):
    def test_thousand_equal_accepted_shares_are_990_user_and_10_fee(self):
        ledger = AcceptedWorkLedger()
        for _ in range(1000):
            ledger.record("fee" if ledger.should_fee() else "user", 1)
        self.assertEqual((ledger.user_shares, ledger.fee_shares), (990, 10))
        self.assertEqual(ledger.snapshot()["fee_percentage"], 1.0)

    def test_vardiff_counts_work_not_shares(self):
        ledger = AcceptedWorkLedger(min_user_work=99)
        for _ in range(99):
            ledger.record("user", 1)
        self.assertTrue(ledger.should_fee())
        ledger.record("fee", Decimal("0.25"))
        self.assertTrue(ledger.should_fee())
        for _ in range(3):
            ledger.record("fee", Decimal("0.25"))
        self.assertFalse(ledger.should_fee())
        self.assertEqual(ledger.snapshot()["fee_percentage"], 1.0)
        self.assertEqual((ledger.user_shares, ledger.fee_shares), (99, 4))

    def test_decimal_difficulty_has_no_accumulating_float_error(self):
        ledger = AcceptedWorkLedger()
        for _ in range(1000):
            ledger.record("fee" if ledger.should_fee() else "user", "0.0001")
        self.assertEqual(ledger.user_work, Fraction(99, 1000))
        self.assertEqual(ledger.fee_work, Fraction(1, 1000))
        report = ledger.snapshot()
        self.assertEqual(report["user_accepted_difficulty"], "0.099")
        self.assertEqual(report["fee_accepted_difficulty"], "0.001")
        self.assertEqual(json.loads(json.dumps(report)), report)

    def test_unavailable_fee_pool_creates_no_outage_debt(self):
        ledger = AcceptedWorkLedger()
        for _ in range(1000):
            self.assertFalse(ledger.should_fee(fee_available=False))
            ledger.record("user", 500, fee_available=False)
        self.assertFalse(ledger.should_fee())
        self.assertEqual(ledger.eligible_user_work, 0)
        self.assertEqual(ledger.snapshot()["outage_user_difficulty"], "500000")
        for _ in range(1000):
            ledger.record("fee" if ledger.should_fee() else "user", 1)
        self.assertEqual(ledger.fee_work, 10)
        self.assertEqual(ledger.snapshot()["eligible_fee_percentage"], 1.0)
        self.assertLess(ledger.snapshot()["fee_percentage"], 1.0)

    def test_rejected_and_late_pool_responses(self):
        ledger = AcceptedWorkLedger()
        ledger.record("user", 99)
        ledger.record("fee", 100, accepted=False)
        self.assertTrue(ledger.should_fee())
        ledger.record("fee", 1, fee_available=False)
        self.assertFalse(ledger.should_fee())
        self.assertEqual((ledger.user_shares, ledger.fee_shares), (1, 1))
        # Already in-flight work must be visible, even if it exceeds the target.
        ledger.record("fee", 1, fee_available=False)
        report = ledger.snapshot()
        self.assertGreater(report["fee_percentage"], 1.0)
        self.assertEqual(report["fee_excess_difficulty"], "1")
        self.assertFalse(ledger.should_fee())

    def test_windows_batch_and_return_to_user_until_deficit_returns(self):
        ledger = AcceptedWorkLedger(min_user_work=99)
        for _ in range(98):
            ledger.record("user", 1)
            self.assertFalse(ledger.should_fee())
        ledger.record("user", 1)
        self.assertTrue(ledger.should_fee())
        ledger.record("fee", Fraction(1, 2))
        self.assertTrue(ledger.should_fee())
        ledger.record("fee", Fraction(1, 2))
        self.assertFalse(ledger.should_fee())
        ledger.record("user", 1)
        self.assertFalse(ledger.should_fee())
        ledger.record("user", 98)
        self.assertTrue(ledger.should_fee())

    def test_larger_fee_share_is_reported_and_compensated(self):
        ledger = AcceptedWorkLedger()
        ledger.record("user", 99)
        ledger.record("fee", 10)
        self.assertFalse(ledger.should_fee())
        self.assertEqual(ledger.snapshot()["fee_excess_difficulty"], "9")
        ledger.record("user", 891)
        self.assertEqual(ledger.snapshot()["fee_percentage"], 1.0)
        self.assertFalse(ledger.should_fee())
        ledger.record("user", 1)
        self.assertTrue(ledger.should_fee())

    def test_invalid_values_never_mutate_ledger(self):
        ledger = AcceptedWorkLedger()
        empty = ledger.snapshot()
        for value in (0, -1, True, None, float("nan"), float("inf"),
                      "NaN", "Infinity", "1e99999999", "-0.1", {}, "x"):
            with self.assertRaises(ValueError, msg=repr(value)):
                ledger.record("user", value)
            self.assertEqual(ledger.snapshot(), empty)
        for target in ("other", None, 0):
            with self.assertRaises(ValueError):
                ledger.record(target, 1)
        with self.assertRaises(ValueError):
            ledger.record("user", 1, accepted=1)
        with self.assertRaises(ValueError):
            ledger.record("user", 1, fee_available=1)
        with self.assertRaises(ValueError):
            ledger.should_fee(fee_available=1)
        with self.assertRaises(ValueError):
            AcceptedWorkLedger(-1)


if __name__ == "__main__":
    unittest.main()
