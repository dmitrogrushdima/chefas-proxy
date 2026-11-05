import unittest
from devfee_scheduler import FeeHealth


class FeeHealthTests(unittest.TestCase):
    def test_outage_recovery_and_exact_deadline(self):
        now = [0.0]
        policy = FeeHealth(10, lambda: now[0])
        now[0] = 9
        self.assertFalse(policy.expired())
        policy.observe(True)
        now[0] = 100
        self.assertFalse(policy.expired())
        policy.observe(False)
        now[0] = 110
        self.assertTrue(policy.expired())

    def test_socket_does_not_clear_rejections_but_acceptance_does(self):
        now = [0.0]
        policy = FeeHealth(10, lambda: now[0])
        policy.observe(True)
        policy.share_result(False)
        now[0] = 10
        policy.observe(True)
        self.assertTrue(policy.expired())
        policy.share_result(True)
        self.assertFalse(policy.expired())
