import unittest
from lab.pow import compact_target, hash_header, meets_target


class ProofOfWorkTests(unittest.TestCase):
    def test_bitcoin_genesis_header(self):
        header = bytes.fromhex(
            "01000000" + "00" * 32 +
            "3ba3edfd7a7b12b27ac72c3e67768f617fc81bc3888a51323a9fb8aa4b1e5e4a" +
            "29ab5f49ffff001d1dac2b7c")
        self.assertEqual(hash_header(header)[::-1].hex(),
                         "000000000019d6689c085ae165831e934ff763ae46a2a6c172b3f1b60a8ce26f")
        target = compact_target(0x1d00ffff)
        self.assertTrue(meets_target(header, target))
        self.assertFalse(meets_target(header, 1))

    def test_invalid_targets_and_header(self):
        for bits in (0, 0x1d80ffff, 0x2300ffff):
            with self.assertRaises(ValueError):
                compact_target(bits)
        with self.assertRaises(ValueError):
            hash_header(b"short")
        with self.assertRaises(ValueError):
            meets_target(bytes(80), 0)
