import unittest

from devfee_scheduler import FeeSimulation


class FeeSimulationTests(unittest.TestCase):
    def test_equal_work_1000_accepted_shares(self):
        model = FeeSimulation()
        for _ in range(1000):
            route = model.select()
            model.record(route)
        self.assertEqual((model.user_work, model.fee_work), (990, 10))

    def test_unavailable_period_does_not_create_fee_debt(self):
        model = FeeSimulation()
        for _ in range(1000):
            self.assertEqual(model.select(fee_available=False), "user")
            model.record("user", fee_available=False)
        self.assertEqual(model.select(), "user")
        self.assertEqual(model.eligible_user_work, 0)
        self.assertEqual(model.fee_work, 0)

    def test_variable_work_can_overshoot_target(self):
        model = FeeSimulation()
        model.record("user", 100)
        self.assertEqual(model.select(), "fee")
        model.record("fee", 50)
        self.assertEqual(model.select(), "user")
        self.assertEqual((model.user_work, model.fee_work), (100, 50))

    def test_invalid_work_and_routes(self):
        model = FeeSimulation()
        for value in (0, -1, 1.5, True):
            with self.assertRaises(ValueError):
                model.record("user", value)
        with self.assertRaises(ValueError):
            model.record("unknown", 1)
