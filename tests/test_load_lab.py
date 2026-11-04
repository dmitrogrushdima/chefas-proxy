import unittest

from lab.load_test import percentile, run


class LoadLabTests(unittest.IsolatedAsyncioTestCase):
    def test_percentiles_and_empty_input(self):
        self.assertIsNone(percentile([], .95))
        self.assertEqual(percentile([4, 1, 3, 2], .5), 2)
        self.assertEqual(percentile([4, 1, 3, 2], .95), 4)

    async def test_loopback_load_experiment(self):
        result = await run(clients=12, ramp_ms=0, timeout=5)
        self.assertEqual(result["successful_echoes"], 12)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["active_at_checkpoint"], 12)
        self.assertFalse(result["production_result"])

    async def test_invalid_experiment_fails_before_listening(self):
        with self.assertRaises(ValueError):
            await run(clients=0, ramp_ms=0, timeout=5)
