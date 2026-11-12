import unittest
from aggregation import (virtual_subscription_job, InsufficientNonceSpace, VirtualPrefixAllocator,
                         ExtranonceSizeCache, AggregationKey)
from pow_validator import build_stratum_header

class VirtualSubscriptionTests(unittest.TestCase):
    def test_work_suffix_preserves_exact_pool_header(self):
        job = ['job', '00' * 32, '010203', '040506', [], '20000000', '1d00ffff', '65000000', True]
        translated = virtual_subscription_job(job, 'aabbccdd', 8, '17', 3,
                                              extranonce2_suffix='00000001')
        self.assertEqual(build_stratum_header(translated, '17', '010203', '65000000', '00000042'),
                         build_stratum_header(job, 'aabbccdd', '1701020300000001', '65000000', '00000042'))
        original = virtual_subscription_job(job, 'aabbccdd', 8, '17', 3)
        self.assertNotEqual(build_stratum_header(original, '17', '010203', '65000000', '00000042'),
                            build_stratum_header(translated, '17', '010203', '65000000', '00000042'))
        for invalid in ('000001', '0000000001', 'zzzzzzzz'):
            with self.assertRaises(ValueError):
                virtual_subscription_job(job, 'aabbccdd', 8, '17', 3, extranonce2_suffix=invalid)

    def test_pool_size_hints_are_scoped_and_expire(self):
        now = [0]
        cache = ExtranonceSizeCache(capacity=1, ttl=10, clock=lambda: now[0])
        first = AggregationKey('pool.example', 3333, False, 'worker', 'x')
        other = AggregationKey('pool.example', 3333, False, 'other', 'x')
        cache.remember(first, 4)
        self.assertEqual(cache.get(first), 4)
        self.assertIsNone(cache.get(other))
        now[0] = 10
        self.assertIsNone(cache.get(first))
        cache.remember(first, 4)
        cache.remember(other, 8)
        self.assertIsNone(cache.get(first))
        self.assertEqual(cache.get(other), 8)

    def test_prefixes_never_reused_and_width_checked(self):
        allocator = VirtualPrefixAllocator(1, 3, maximum_workers=2)
        self.assertTrue(allocator.compatible(4))
        self.assertTrue(allocator.compatible(5))
        self.assertFalse(allocator.compatible(3))
        first = allocator.allocate('one')
        self.assertEqual(first, allocator.allocate('one'))
        self.assertNotEqual(first, allocator.allocate('two'))
        with self.assertRaises(InsufficientNonceSpace):
            allocator.allocate('three')
        allocator.release('one')
        self.assertNotEqual(first, allocator.allocate('three'))
        allocator.release('two')
        allocator.release('three')
        for number in range(253):
            name = str(number)
            allocator.allocate(name)
            allocator.release(name)
        with self.assertRaises(InsufficientNonceSpace):
            allocator.allocate('exhausted')

    def test_coinbase_and_header_match_pool(self):
        job = ['job', '00' * 32, '010203', '040506', [], '20000000', '1d00ffff', '65000000', True]
        translated = virtual_subscription_job(job, 'aabbccdd', 4, '17', 3)
        pool_header = build_stratum_header(job, 'aabbccdd', '17010203', '65000000', '00000042')
        miner_header = build_stratum_header(translated, '17', '010203', '65000000', '00000042')
        self.assertEqual(pool_header, miner_header)
        padded = virtual_subscription_job(job, 'aabbccdd', 8, '17', 3)
        padded_header = build_stratum_header(padded, '17', '010203', '65000000', '00000042')
        self.assertEqual(padded_header, build_stratum_header(
            job, 'aabbccdd', '1701020300000000', '65000000', '00000042'))
        self.assertEqual(job[2], '010203')
        with self.assertRaises(InsufficientNonceSpace):
            virtual_subscription_job(job, 'aabbccdd', 4, '1723', 3)
