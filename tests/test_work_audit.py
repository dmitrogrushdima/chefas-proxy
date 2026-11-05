import unittest
from decimal import Decimal
from devfee_scheduler import AcceptedWorkLedger
from observer import WorkAudit


class AuditTests(unittest.TestCase):
    def test_confirmed_difficulty_is_bucketed_and_windows_expire(self):
        now = [1000.0]
        ledger = AcceptedWorkLedger()
        audit = WorkAudit(ledger, lambda: now[0])
        ledger.record('user', Decimal('2'), fee_available=True)
        audit.record('user', Decimal('2'))
        now[0] += 10
        snap = audit.snapshot()
        self.assertEqual(snap['user_accepted_difficulty'], '2')
        self.assertEqual(snap['hashrate_windows']['5m']['hashrate_hs'], 2 * 2**32 / 10)
        self.assertTrue(snap['hashrate_windows']['5m']['warmup'])
        now[0] += 300
        self.assertEqual(audit.snapshot()['hashrate_windows']['5m']['hashrate_hs'], 0)
        self.assertGreater(audit.snapshot()['hashrate_windows']['1h']['hashrate_hs'], 0)
        now[0] += 3600
        self.assertEqual(audit.snapshot()['hashrate_windows']['1h']['hashrate_hs'], 0)
