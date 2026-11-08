"""Cryptographic and search-space invariants, not live socket consolidation."""
import dataclasses
from fractions import Fraction
import unittest
from unittest.mock import patch

from aggregation import (AggregationJob, AggregationKey, InsufficientNonceSpace,
                         LocalVardiff, NonceLease, NoncePartitioner, ShareTargets,
                         choose_local_extranonce2_size, classify_header)
from pow_validator import (DIFF_ONE_TARGET, MAX_TARGET, build_stratum_header,
                           compact_target, hash_header)


def job():
    return ["shared-job", bytes(range(32)).hex(), "010203", "040506",
            ["11" * 32, "22" * 32], "2000e002", "1d00ffff", "65000000", True]


class AggregationMathTests(unittest.TestCase):
    def test_group_identity_separates_accounts_tls_masks_and_agents(self):
        key = AggregationKey("POOL.Example", 3333, True, "alice.farm",
                             "private-password", "1FFFE000", "cgminer/4.11.1")
        same = dataclasses.replace(key, host="pool.example", version_mask="1fffe000")
        self.assertEqual(key, same)
        self.assertEqual(hash(key), hash(same))
        self.assertNotIn("private-password", repr(key))
        changes = dict(host="other.example", port=443, use_tls=False,
                       worker="other-wallet.worker", password="other-secret",
                       version_mask="0000e000", user_agent="different/1",
                       tls_policy="custom-ca-sha256")
        keys = {key}
        for field, value in changes.items():
            changed = dataclasses.replace(key, **{field: value})
            self.assertNotEqual(key, changed)
            keys.add(changed)
        self.assertEqual(len(keys), len(changes) + 1)
        for fields in (dict(host="http request"), dict(port=True), dict(use_tls=1),
                       dict(worker=""), dict(version_mask=" ffffffff"),
                       dict(password="secret\nheader")):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                dataclasses.replace(key, **fields)

    def test_explicit_asic_bounds_can_require_one_to_one_fallback(self):
        with self.assertRaises(InsufficientNonceSpace):
            choose_local_extranonce2_size(4, minimum_local_size=4,
                                         preferred_local_size=4, maximum_workers=100)
        self.assertEqual(choose_local_extranonce2_size(
            4, minimum_local_size=3, preferred_local_size=4), 3)
        self.assertEqual(choose_local_extranonce2_size(
            4, minimum_local_size=4, preferred_local_size=4, maximum_workers=1), 4)
        with self.assertRaises(InsufficientNonceSpace):
            NoncePartitioner("aabb", 4, 4)
        for size in (True, -1, 33, 1.5):
            with self.subTest(size=size), self.assertRaises(ValueError):
                NoncePartitioner("aabb", 4, size)
        with self.assertRaises(ValueError):
            NoncePartitioner("aabb", 4, 3, maximum_workers=10001)

    def test_hundred_workers_have_disjoint_headers_and_preserve_full_pow(self):
        partitions = NoncePartitioner("12345678", 4, 3)
        downstream_headers = set()
        prefixes = set()
        for index in range(100):
            lease = partitions.allocate(str(index))
            prefixes.add(lease.prefix)
            local_extra = "abcdef"
            upstream_extra = lease.upstream_extranonce2(local_extra)
            downstream = build_stratum_header(job(), lease.downstream_extranonce1,
                                               local_extra, "65000000", "00000001",
                                               "00002000", "0000e000")
            upstream = build_stratum_header(job(), "12345678", upstream_extra,
                                             "65000000", "00000001",
                                             "00002000", "0000e000")
            self.assertEqual(downstream, upstream)
            self.assertEqual(hash_header(downstream), hash_header(upstream))
            downstream_headers.add(downstream)
        self.assertEqual(len(prefixes), 100)
        self.assertEqual(len(downstream_headers), 100)
        self.assertEqual(partitions.active_count, 100)
        with self.assertRaises(InsufficientNonceSpace):
            partitions.allocate("101st worker")

    def test_released_prefix_never_reused_and_exhaustion_fails_closed(self):
        partitions = NoncePartitioner("12345678", 4, 3, maximum_workers=2)
        first = partitions.allocate("miner")
        self.assertIs(first, partitions.allocate("miner"))
        self.assertTrue(partitions.release("miner"))
        second = partitions.allocate("miner")
        self.assertNotEqual(first.prefix, second.prefix)
        self.assertEqual(first.upstream_extranonce2("000000"), "00000000")
        self.assertEqual(second.upstream_extranonce2("000000"), "01000000")
        partitions.release("miner")
        for index in range(2, 256):
            lease = partitions.allocate("miner")
            self.assertEqual(lease.prefix, f"{index:02x}")
            partitions.release("miner")
        self.assertEqual(partitions.active_count, 0)
        self.assertEqual(partitions.remaining_prefixes, 0)
        with self.assertRaisesRegex(InsufficientNonceSpace, "fresh upstream"):
            partitions.allocate("miner")
        for value in ("0000", "00000000", "00 000", "xxxxxx", "000000\n"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                first.upstream_extranonce2(value)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            first.prefix = "ff"

    def test_one_worker_zero_width_prefix_and_zero_local_width_are_defined(self):
        single = NoncePartitioner("1234", 4, 4, maximum_workers=1)
        lease = single.allocate("miner")
        self.assertEqual(lease.prefix, "")
        self.assertEqual(lease.downstream_extranonce1, "1234")
        self.assertEqual(lease.upstream_extranonce2("aabbccdd"), "aabbccdd")
        single.release("miner")
        with self.assertRaises(InsufficientNonceSpace):
            single.allocate("miner")
        empty = NoncePartitioner("1234", 1, 0, maximum_workers=100).allocate("miner")
        self.assertEqual(empty.upstream_extranonce2(""), "00")

    def test_vardiff_commits_only_for_new_jobs_with_bounded_adaptation(self):
        vardiff = LocalVardiff(100, 10000, minimum_difficulty=10, alpha=1)
        vardiff.record_valid_share(0, 100)
        vardiff.record_valid_share(1, 100)
        self.assertEqual(vardiff.current_difficulty, 100)
        self.assertEqual(vardiff.new_job(), 200)  # Desired 1000, bounded to 2x.
        self.assertEqual(vardiff.new_job(), 200)  # No new observation.
        vardiff.record_valid_share(1001, 200)
        self.assertEqual(vardiff.new_job(), 100)  # Desired 2, bounded to 0.5x.
        self.assertEqual(vardiff.new_job(pool_difficulty=15), 15)  # Pool cap wins.
        with self.assertRaises(ValueError):
            vardiff.new_job(pool_difficulty=9)
        self.assertEqual(vardiff.current_difficulty, 15)

    def test_vardiff_uses_captured_work_ema_and_bounded_memory(self):
        vardiff = LocalVardiff(100, 1000000, minimum_difficulty=1,
                               target_interval=10, alpha=Fraction(1, 2))
        vardiff.record_valid_share(0, 100)
        vardiff.record_valid_share(10, 100)  # 0.1 seconds per difficulty.
        vardiff.record_valid_share(50, 200)  # 0.2, EMA 0.15.
        self.assertAlmostEqual(float(vardiff.new_job()), 10 / .15)
        prior = vardiff.current_difficulty
        vardiff.record_valid_share(50, 200)  # Same clock tick is not a zero divisor.
        self.assertEqual(vardiff.new_job(), prior)
        for index in range(1, 5001):
            vardiff.record_valid_share(Fraction(150 + index, 3), 73)
            vardiff.new_job()
        self.assertLess(vardiff._seconds_per_diff.numerator.bit_length(), 256)
        self.assertLess(vardiff._seconds_per_diff.denominator.bit_length(), 256)
        self.assertLess(vardiff.current_difficulty.numerator.bit_length(), 256)
        for timestamp, work in ((0, 1), ("NaN", 1), (True, 1), (5000, "Infinity")):
            with self.subTest(timestamp=timestamp, work=work), self.assertRaises(ValueError):
                vardiff.record_valid_share(timestamp, work)

    def test_invalid_difficulty_configuration_is_rejected(self):
        for initial, pool, options in ((0, 10, {}), (11, 10, {}),
                                       (1, 10, {"minimum_difficulty": 2}),
                                       (1, 10, {"alpha": 2}),
                                       (1, 10, {"target_interval": "NaN"}),
                                       (True, 10, {})):
            with self.subTest(initial=initial, pool=pool, options=options), self.assertRaises(ValueError):
                LocalVardiff(initial, pool, **options)

    def test_single_header_hash_and_target_boundary_inclusivity(self):
        digest = (100).to_bytes(32, "little")
        with patch("aggregation.hash_header", return_value=digest) as hashing:
            decision = classify_header(bytes(80), ShareTargets(100, 100, 1))
        hashing.assert_called_once_with(bytes(80))
        self.assertTrue(decision.accept_locally)
        self.assertTrue(decision.forward)
        self.assertFalse(decision.block_candidate)
        with patch("aggregation.hash_header", return_value=(101).to_bytes(32, "little")):
            decision = classify_header(bytes(80), ShareTargets(100, 100, 1))
        self.assertFalse(decision.accept_locally)
        self.assertFalse(decision.forward)

    def test_network_block_is_forwarded_even_above_pool_share_target(self):
        genesis = bytes.fromhex(
            "01000000" + "00" * 32 +
            "3ba3edfd7a7b12b27ac72c3e67768f617fc81bc3888a51323a9fb8aa4b1e5e4a" +
            "29ab5f49ffff001d1dac2b7c")
        decision = classify_header(genesis, ShareTargets(2, 1, DIFF_ONE_TARGET))
        self.assertFalse(decision.local_valid)
        self.assertFalse(decision.upstream_valid)
        self.assertTrue(decision.block_candidate)
        self.assertTrue(decision.forward)
        self.assertTrue(decision.accept_locally)
        self.assertEqual(decision.digest[::-1].hex(),
                         "000000000019d6689c085ae165831e934ff763ae46a2a6c172b3f1b60a8ce26f")

    def test_share_filter_never_combines_low_diff_proofs(self):
        # 100 disjoint search spaces, 100 submits each. The simulated pool checks
        # each forwarded SHA-256d against its own harder target independently.
        partitions = NoncePartitioner("12345678", 4, 3)
        targets = ShareTargets(MAX_TARGET, MAX_TARGET // 128, DIFF_ONE_TARGET)
        forwarded = 0
        for worker in range(100):
            lease = partitions.allocate(str(worker))
            captured = AggregationJob(job(), lease, targets, "0000e000")
            for nonce in range(100):
                nonce_hex = f"{nonce:08x}"
                decision = captured.classify_submit("abcdef", "65000000", nonce_hex, "00002000")
                self.assertTrue(decision.accept_locally)
                if decision.forward:
                    pool_header = build_stratum_header(
                        job(), "12345678", lease.upstream_extranonce2("abcdef"),
                        "65000000", nonce_hex, "00002000", "0000e000")
                    self.assertLessEqual(int.from_bytes(hash_header(pool_header), "little"), targets.upstream)
                    forwarded += 1
        self.assertGreater(forwarded, 0)
        self.assertLess(forwarded, 200)

    def test_job_snapshot_preserves_merkle_version_and_difficulty_context(self):
        partitions = NoncePartitioner("12345678", 4, 3)
        params = job()
        lease = partitions.allocate("miner")
        targets = ShareTargets.from_difficulties("0.000001", 2, params[6])
        captured = AggregationJob(params, lease, targets, "0000e000")
        first = captured.classify_submit("abcdef", "65000000", "12345678", "00002000")
        params[4].append("33" * 32)
        params[5] = "ffffffff"
        params[6] = "1b0404cb"
        partitions.release("miner")
        partitions.allocate("miner")
        self.assertEqual(first, captured.classify_submit(
            "abcdef", "65000000", "12345678", "00002000"))
        self.assertEqual(captured.targets.upstream, DIFF_ONE_TARGET // 2)
        with self.assertRaisesRegex(ValueError, "missing version"):
            captured.classify_submit("abcdef", "65000000", "12345678")
        with self.assertRaisesRegex(ValueError, "exceed"):
            captured.classify_submit("abcdef", "65000000", "12345678", "00010000")
        with self.assertRaises(ValueError):
            captured.classify_submit("abcd", "65000000", "12345678", "00002000")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            captured.targets = ShareTargets(MAX_TARGET, MAX_TARGET, DIFF_ONE_TARGET)

    def test_malformed_or_inconsistent_targets_and_snapshots_fail_closed(self):
        for values in ((0, 1, 1), (1, 2, 1), (MAX_TARGET + 1, 1, 1),
                       (True, 1, 1), (1, 1, -1)):
            with self.subTest(values=values), self.assertRaises(ValueError):
                ShareTargets(*values)
        with self.assertRaises(ValueError):
            ShareTargets.from_difficulties(2, 1, "1d00ffff")
        lease = NoncePartitioner("12", 4, 3).allocate("miner")
        targets = ShareTargets(100, 1, DIFF_ONE_TARGET)
        for params in ([], job()[:8], job()[:4] + [[[]]] + job()[5:]):
            with self.subTest(params=params), self.assertRaises(ValueError):
                AggregationJob(params, lease, targets)
        with self.assertRaisesRegex(ValueError, "network target"):
            AggregationJob(job(), lease, ShareTargets(100, 1, compact_target(0x1b0404cb)))
        with self.assertRaises(ValueError):
            NonceLease("worker", "12", 4, "00", 4)


if __name__ == "__main__":
    unittest.main()
