import unittest
from aggregation import NoncePartitioner
from router import AggregatedJobRouter

class AggregatedRoutingTests(unittest.TestCase):
    def test_job_isolation_filter_and_expiry(self):
        lease = NoncePartitioner('aabb', 4, 3, maximum_workers=2).allocate('one')
        router = AggregatedJobRouter(lease, ttl=10)
        job = ['pool-job', '00' * 32, '010203', '040506', [], '20000000', '1d00ffff', '65000000', True]
        issued = router.issue(job, '0.000000001', '0.00000001', now=0)
        self.assertNotEqual(issued[0], job[0])
        forwarded = locally_accepted = 0
        for nonce in range(500):
            params = ['worker', issued[0], '010203', '65000000', f'{nonce:08x}']
            try:
                decision, upstream = router.submit(params, now=1)
            except ValueError as exc:
                self.assertEqual(str(exc), 'low difficulty')
                continue
            locally_accepted += 1
            if decision.forward:
                forwarded += 1
                self.assertEqual(upstream[1], 'pool-job')
                self.assertEqual(upstream[2], lease.prefix + '010203')
            else:
                self.assertIsNone(upstream)
            with self.assertRaisesRegex(ValueError, 'duplicate'):
                router.submit(params, now=2)
        self.assertGreater(forwarded, 0)
        self.assertGreater(locally_accepted, forwarded)
        with self.assertRaisesRegex(ValueError, 'unknown job'):
            router.submit(['worker', issued[0], '010203', '65000000', '00000000'], now=11)
