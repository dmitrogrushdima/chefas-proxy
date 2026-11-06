"""Exact fleet budget tests; these do not claim 5,000 live socket capacity."""

from fractions import Fraction
import unittest

from devfee_scheduler import AcceptedWorkLedger, FairnessAllocator, StableFeeSlots, stable_fee_worker


class FleetAllocatorTests(unittest.TestCase):
    def test_five_thousand_eligible_miners_share_one_budget(self):
        ledger = AcceptedWorkLedger()
        fleet = FairnessAllocator(ledger, max_active=5000)
        for _ in range(5000):
            ledger.record('user', 99)
        admitted = [worker for worker in range(5000) if fleet.acquire(worker, 100)]
        self.assertEqual(len(admitted), 50)
        self.assertEqual(fleet.reserved, 5000)
        tokens = [fleet.begin_submit(worker, 100) for worker in admitted]
        self.assertEqual(fleet.snapshot()['active_grants'], 0)
        self.assertEqual(fleet.snapshot()['pending_submissions'], 50)
        for token in reversed(tokens):
            ledger.record('fee', 100)
            fleet.finish_submit(token)
        self.assertEqual(fleet.reserved, 0)
        self.assertEqual(fleet.snapshot()['pending_submissions'], 0)
        self.assertEqual(ledger.snapshot()['fee_percentage'], 1.0)
        self.assertFalse(fleet.acquire(9999, 100))

    def test_indivisible_final_grant_not_one_excess_for_each_worker(self):
        ledger = AcceptedWorkLedger()
        ledger.record('user', 99)
        fleet = FairnessAllocator(ledger, max_active=5000)
        self.assertTrue(fleet.acquire('large', 100))
        self.assertFalse(any(fleet.acquire(worker, 100) for worker in range(5000)))
        token = fleet.begin_submit('large', 100)
        ledger.record('fee', 100)
        fleet.finish_submit(token)
        self.assertEqual(ledger.snapshot()['fee_excess_difficulty'], '99')
        self.assertFalse(fleet.acquire('small', Fraction(1, 100)))
        ledger.record('user', 99 * 99)
        self.assertEqual(ledger.snapshot()['fee_percentage'], 1.0)

    def test_vardiff_and_pending_transfer_preserve_exact_reservations(self):
        ledger = AcceptedWorkLedger()
        ledger.record('user', 990)
        fleet = FairnessAllocator(ledger)
        self.assertTrue(fleet.acquire('a', '2.5'))
        self.assertTrue(fleet.acquire('a', '3.25'))
        self.assertEqual(fleet.reserved, Fraction(13, 4))
        token = fleet.begin_submit('a', '2.5')  # old job at the earlier difficulty
        self.assertFalse(fleet.active('a'))
        self.assertEqual(fleet.reserved, Fraction(5, 2))
        self.assertFalse(fleet.acquire('a', 1))
        fleet.release('a')
        self.assertEqual(fleet.reserved, Fraction(5, 2))
        ledger.record('fee', '2.5')
        fleet.finish_submit(token)
        self.assertEqual(fleet.reserved, 0)
        self.assertTrue(fleet.acquire('a', 1))
        self.assertEqual(ledger.snapshot()['fee_accepted_difficulty'], '2.5')

    def test_late_proofs_survive_release_and_are_counted_as_actual_work(self):
        ledger = AcceptedWorkLedger()
        ledger.record('user', 99)
        fleet = FairnessAllocator(ledger)
        self.assertTrue(fleet.acquire('old', 1))
        token = fleet.begin_submit('old', 1)
        fleet.release('old')
        late = fleet.begin_submit('old', 2)
        self.assertEqual(fleet.reserved, 3)
        ledger.record('fee', 2)
        fleet.finish_submit(late)
        self.assertEqual(fleet.reserved, 1)
        self.assertFalse(fleet.acquire('new', 1))
        ledger.record('fee', 1)
        fleet.finish_submit(token)
        self.assertEqual(ledger.fee_work, 3)
        self.assertEqual(fleet.reserved, 0)

    def test_response_outcomes_and_double_settlement(self):
        ledger = AcceptedWorkLedger()
        ledger.record('user', 99)
        fleet = FairnessAllocator(ledger)
        token = fleet.begin_submit(1, 1)
        before = fleet.snapshot()
        with self.assertRaises(ValueError):
            fleet.finish_submit(token, 'fabricated')
        self.assertEqual(fleet.snapshot(), before)
        fleet.finish_submit(token, 'rejected')
        token2 = fleet.begin_submit(1, 1)
        fleet.finish_submit(token2, 'unknown')
        with self.assertRaises(ValueError):
            fleet.finish_submit(token2)
        self.assertEqual(fleet.snapshot()['rejected_submissions'], 1)
        self.assertEqual(fleet.snapshot()['unknown_submissions'], 1)
        self.assertEqual(fleet.snapshot()['unknown_difficulty'], '1')
        self.assertEqual(ledger.fee_work, 0)
        self.assertTrue(fleet.acquire(2, 1))

    def test_expired_global_debt_revokes_idle_grants(self):
        ledger = AcceptedWorkLedger()
        ledger.record('user', 396)
        fleet = FairnessAllocator(ledger)
        for worker in range(4):
            self.assertTrue(fleet.acquire(worker, 1))
        ledger.record('fee', 2)  # earlier, already in-flight work
        self.assertFalse(fleet.should_continue(0))
        self.assertFalse(fleet.acquire(0, 1))
        self.assertFalse(fleet.acquire(1, 1))
        self.assertTrue(fleet.acquire(2, 1))
        self.assertTrue(fleet.acquire(3, 1))
        self.assertEqual(fleet.snapshot()['active_grants'], 2)
        self.assertEqual(fleet.reserved, 2)

    def test_outages_capacity_and_invalid_input_do_not_mutate_state(self):
        ledger = AcceptedWorkLedger()
        ledger.record('user', 99000, fee_available=False)
        fleet = FairnessAllocator(ledger, max_active=1, max_pending=1)
        self.assertFalse(fleet.acquire(1, 1))
        ledger.record('user', 198)
        self.assertTrue(fleet.acquire(1, 1))
        self.assertFalse(fleet.acquire(2, 1))
        token = fleet.begin_submit(1, 1)
        before = fleet.snapshot()
        with self.assertRaises(ValueError):
            fleet.begin_submit(2, 1)
        self.assertEqual(fleet.snapshot(), before)
        for invalid in (False, None, '', -1, 'x' * 257):
            with self.assertRaises(ValueError):
                fleet.acquire(invalid, 1)
        for invalid in (0, -1, float('inf'), True):
            with self.assertRaises(ValueError):
                fleet.acquire(1, invalid)
        self.assertEqual(fleet.snapshot(), before)
        fleet.finish_submit(token, 'unknown')
        self.assertTrue(fleet.acquire(2, 1))
        fleet.release(2)
        fleet.release(2)
        self.assertEqual(fleet.reserved, 0)

    def test_worker_suffix_is_operator_explicit_and_bounded(self):
        self.assertEqual(stable_fee_worker('wallet.worker', 7), 'wallet.worker')
        self.assertEqual(stable_fee_worker('wallet.worker', 7, '{worker}_{slot}'), 'wallet.worker_7')
        for template in ('other_wallet.{slot}', '{worker.__class__}', '{worker!r}',
                         '{worker:10000}', '{worker}\n{slot}', '{worker}_{bad}'):
            with self.assertRaises(ValueError):
                stable_fee_worker('wallet.worker', 7, template)
        with self.assertRaises(ValueError):
            stable_fee_worker('a' * 128, 10000, '{worker}_{slot}')
        for slot in (0, -1, 10001, True):
            with self.assertRaises(ValueError):
                stable_fee_worker('wallet.worker', slot)

    def test_stable_slots_reuse_only_released_owners_across_long_uptime(self):
        slots = StableFeeSlots(3)
        self.assertEqual(slots.obtain('a'), 1)
        self.assertEqual(slots.obtain('b'), 2)
        self.assertEqual(slots.obtain('a'), 1)
        self.assertEqual(slots.obtain('c'), 3)
        with self.assertRaises(ValueError):
            slots.obtain('overflow')
        self.assertEqual(slots.obtain('b'), 2)
        slots.release('b')
        slots.release('b')
        for session in range(10001, 30001):
            self.assertEqual(slots.obtain(session), 2)
            self.assertEqual(len({slots.obtain('a'), slots.obtain('c'), slots.obtain(session)}), 3)
            slots.release(session)
        self.assertEqual(slots.obtain('replacement'), 2)
        self.assertEqual(len(slots._owners), 3)
        self.assertLessEqual(len(slots._free), 3)

    def test_stable_slot_inputs_are_bounded(self):
        for invalid in (0, 10001, -1, True, 1.5):
            with self.assertRaises(ValueError):
                StableFeeSlots(invalid)
        slots = StableFeeSlots(1)
        for invalid in (None, False, -1, '', 'x' * 257):
            with self.assertRaises(ValueError):
                slots.obtain(invalid)
        self.assertEqual(slots.obtain(10**30), 1)


if __name__ == '__main__':
    unittest.main()
