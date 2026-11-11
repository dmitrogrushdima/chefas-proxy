"""Router regressions for fee reservations, authorization and slot cleanup."""

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from devfee_scheduler import AcceptedWorkLedger, FairnessAllocator, StableFeeSlots
from observer import Metrics, WorkerRegistry, WorkAudit
from router import StatefulRouter


class FleetRouterGuardTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self):
        metrics = Metrics()
        ledger = AcceptedWorkLedger()
        workers = WorkerRegistry()
        workers.authorize(1, 'worker', '127.0.0.1')
        relay = SimpleNamespace(
            fee=('fee.test', 3333), fee_worker='account.worker',
            fee_worker_template=None, fee_slots=StableFeeSlots(1),
            fee_jitter_seconds=0, fee_allocator=FairnessAllocator(ledger),
            ledger=ledger, metrics=metrics, workers=workers, validate_pow=False,
            observe_connection=Mock(),
        )
        metrics._audit = WorkAudit(ledger)
        session = {'id': 1, 'metrics': metrics}
        return relay, session

    def channel(self, target, *, authorized=True):
        ready = asyncio.Event()
        ready.set()
        return SimpleNamespace(
            target=target, closed=False, authorized=authorized, ready=ready,
            pool_label='pool.test:3333',
            extranonce1='11', extranonce2_size=4, job_difficulty=2,
            request=AsyncMock(return_value={'result': True, 'error': None}),
        )

    async def test_invalid_expanded_worker_releases_constructor_slot(self):
        relay, session = self.fixture()
        relay.fee_worker = 'w' * 128
        relay.fee_worker_template = '{worker}_{slot}'
        with self.assertRaises(ValueError):
            StatefulRouter(relay, None, None, session)
        # A failed construction never reaches run().finally. Its slot must
        # nevertheless be available to the next otherwise valid session.
        self.assertEqual(relay.fee_slots.obtain('replacement'), 1)
        with self.assertRaises(ValueError):
            relay.fee_slots.obtain('second-live-owner')
        relay.fee_slots.release('replacement')

    async def test_unauthorized_fee_ready_event_does_not_charge_outage_work(self):
        relay, session = self.fixture()
        relay.ledger.record('user', 99)
        router = StatefulRouter(relay, None, None, session)
        router.auth['worker'] = 'x'
        user = self.channel('user')
        fee = self.channel('fee', authorized=False)
        router.current = user
        router.channels = {'user': user, 'fee': fee}
        router.activate = AsyncMock(return_value=True)
        router.reply = AsyncMock()
        router.jobs['job'] = (user, 'upstream-job', [], '11', 4, 2, None)

        await router.reconcile()
        self.assertIs(router.current, user)
        self.assertFalse(relay.fee_allocator.active(session['id']))
        router.activate.assert_not_awaited()

        await router.submit(1, ['worker', 'job', '00000000', '65000000', '00000000'])
        self.assertEqual(relay.ledger.user_work, 101)
        self.assertEqual(relay.ledger.eligible_user_work, 99)
        self.assertEqual(relay.ledger.snapshot()['outage_user_difficulty'], '2')
        self.assertEqual(relay.ledger.fee_work, 0)
        self.assertIs(router.current, user)
        self.assertEqual(session['pending'], 0)
        self.assertFalse(relay.fee_allocator.active(session['id']))
        router.activate.assert_not_awaited()
        user.request.assert_awaited_once()

    async def test_accepted_fee_rpc_survives_failed_asic_ack_without_unknown_credit(self):
        relay, session = self.fixture()
        relay.ledger.record('user', 198)
        router = StatefulRouter(relay, None, None, session)
        router.auth['worker'] = 'x'
        fee = self.channel('fee')
        router.current = fee
        router.channels['fee'] = fee
        router.jobs['job'] = (fee, 'upstream-job', [], '11', 4, 2, None)
        self.assertTrue(relay.fee_allocator.acquire(session['id'], 2))

        async def confirm(method, params):
            self.assertEqual(method, 'mining.submit')
            self.assertEqual(params[0], relay.fee_worker)
            self.assertEqual(params[1], 'upstream-job')
            self.assertEqual(relay.fee_allocator.snapshot()['pending_submissions'], 1)
            self.assertEqual(relay.fee_allocator.reserved, 2)
            self.assertFalse(relay.fee_allocator.active(session['id']))
            return {'result': True, 'error': None}

        fee.request = AsyncMock(side_effect=confirm)
        router.reply = AsyncMock(side_effect=ConnectionError('ASIC disappeared after pool acceptance'))
        with self.assertRaises(ConnectionError):
            await router.submit(1, ['worker', 'job', '00000000', '65000000', '00000000'])

        allocation = relay.fee_allocator.snapshot()
        self.assertEqual(allocation['accepted_submissions'], 1)
        self.assertEqual(allocation['unknown_submissions'], 0)
        self.assertEqual(allocation['unknown_difficulty'], '0')
        self.assertEqual(allocation['pending_submissions'], 0)
        self.assertEqual(relay.fee_allocator.reserved, 0)
        self.assertEqual(relay.ledger.fee_work, 2)
        self.assertEqual((relay.metrics.accepted, relay.metrics.unmatched), (1, 0))
        row = relay.workers.snapshot()['workers'][0]
        self.assertEqual((row['accepted'], row['unknown']), (1, 0))
        self.assertEqual(session['pending'], 0)
        fee.request.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
