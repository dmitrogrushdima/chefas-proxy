import unittest
from observer import WorkerRegistry


class WorkerRegistryTests(unittest.TestCase):
    def test_reconnect_identity_and_source_ip_isolation(self):
        now = [1000.0]
        registry = WorkerRegistry(clock=lambda: now[0])
        registry.authorize(1, 'alice', '::ffff:192.0.2.1')
        registry.authorize(2, 'alice', '192.0.2.1')
        registry.authorize(3, 'alice', '192.0.2.2')
        registry.record(1, 'alice', 'accepted', difficulty='2.5', rtt_ms=12)
        registry.record(2, 'alice', 'rejected', rtt_ms=8, stale=True)
        now[0] += 100
        rows = {r['ip']: r for r in registry.snapshot()['workers']}
        self.assertEqual(rows['192.0.2.1']['connection_count'], 2)
        self.assertEqual(rows['192.0.2.1']['accepted'], 1)
        self.assertEqual(rows['192.0.2.1']['stale'], 1)
        self.assertEqual(rows['192.0.2.1']['rejected'], 1)
        self.assertEqual(rows['192.0.2.1']['pool_response_ms'], 10)
        self.assertIsNone(rows['192.0.2.1']['asic_ping_ms'])
        self.assertAlmostEqual(rows['192.0.2.1']['hashrate_windows']['15m']['hashrate_hs'], 2.5 * 2**32 / 100)
        self.assertIsNone(rows['192.0.2.2']['hashrate_windows'])
        identity = rows['192.0.2.1']['id']
        registry.disconnect(1)
        self.assertEqual(next(r for r in registry.snapshot()['workers'] if r['id'] == identity)['status'], 'online')
        registry.disconnect(2)
        self.assertEqual(next(r for r in registry.snapshot()['workers'] if r['id'] == identity)['status'], 'offline')
        registry.authorize(4, 'alice', '192.0.2.1')
        row = next(r for r in registry.snapshot()['workers'] if r['id'] == identity)
        self.assertEqual((row['accepted'], row['status']), (1, 'online'))

    def test_windows_expire_and_local_rejects_do_not_increase_work(self):
        now = [0.0]
        registry = WorkerRegistry(clock=lambda: now[0])
        registry.authorize(1, 'worker', '127.0.0.1')
        registry.record(1, 'worker', 'accepted', difficulty=1)
        registry.record(1, 'worker', 'invalid_local')
        now[0] = 901
        row = registry.snapshot()['workers'][0]
        self.assertEqual(row['hashrate_windows']['15m']['hashrate_hs'], 0)
        self.assertGreater(row['hashrate_windows']['1h']['hashrate_hs'], 0)
        now[0] = 3601
        self.assertEqual(registry.snapshot()['workers'][0]['hashrate_windows']['1h']['hashrate_hs'], 0)
        self.assertGreater(registry.snapshot()['workers'][0]['hashrate_windows']['24h']['hashrate_hs'], 0)
        now[0] = 86401
        self.assertEqual(registry.snapshot()['workers'][0]['hashrate_windows']['24h']['hashrate_hs'], 0)
        registry.disconnect(1)
        now[0] += 86400
        self.assertFalse(registry.snapshot()['workers'])

    def test_capacity_only_evicts_offline_rows_and_reports_dropped_data(self):
        registry = WorkerRegistry(max_workers=1)
        registry.authorize(1, 'a', '127.0.0.1')
        registry.authorize(2, 'b', '127.0.0.1')
        self.assertEqual(registry.snapshot()['dropped_observations'], 1)
        self.assertEqual(registry.snapshot()['workers'][0]['worker'], 'a')
        registry.disconnect(1)
        registry.authorize(3, 'b', '127.0.0.1')
        self.assertEqual(registry.snapshot()['workers'][0]['worker'], 'b')

    def test_multiple_workers_on_one_socket_keep_separate_accounting(self):
        registry = WorkerRegistry()
        registry.authorize(7, 'a', '127.0.0.1')
        registry.authorize(7, 'b', '127.0.0.1')
        registry.record(7, 'a', 'accepted', difficulty=1)
        registry.record(7, 'b', 'rejected', stale=True)
        rows = {row['worker']: row for row in registry.snapshot()['workers']}
        self.assertEqual((rows['a']['accepted'], rows['a']['rejected']), (1, 0))
        self.assertEqual((rows['b']['accepted'], rows['b']['rejected'], rows['b']['stale']), (0, 1, 1))
        registry.disconnect(7)
        self.assertTrue(all(row['status'] == 'offline' for row in registry.snapshot()['workers']))
