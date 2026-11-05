import unittest
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from router import StatefulRouter
from observer import Metrics, WorkerRegistry, WorkAudit
from devfee_scheduler import AcceptedWorkLedger


class ConfirmedOutcomeTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_asic_ack_does_not_double_count_pool_confirmation(self):
        metrics = Metrics()
        ledger = AcceptedWorkLedger()
        workers = WorkerRegistry()
        workers.authorize(1, 'worker', '127.0.0.1')
        relay = SimpleNamespace(fee=None, fee_owner=None, ledger=ledger, metrics=metrics,
                                workers=workers, validate_pow=False)
        metrics._audit = WorkAudit(ledger)
        session = {'id': 1, 'metrics': metrics}
        router = StatefulRouter(relay, None, None, session)
        router.auth['worker'] = 'x'
        channel = SimpleNamespace(target='user', closed=False, extranonce1='11', extranonce2_size=4,
                                  request=AsyncMock(return_value={'result': True, 'error': None}))
        router.jobs['job'] = (channel, 'upstream-job', [], '11', 4, 2, None)
        router.reply = AsyncMock(side_effect=ConnectionError('ASIC disappeared'))
        with self.assertRaises(ConnectionError):
            await router.submit(1, ['worker', 'job', '00000000', '65000000', '00000000'])
        self.assertEqual((metrics.accepted, metrics.unmatched), (1, 0))
        row = workers.snapshot()['workers'][0]
        self.assertEqual((row['accepted'], row['unknown']), (1, 0))
        self.assertEqual(ledger.user_work, 2)
        self.assertEqual(session['pending'], 0)

    async def test_shutdown_marks_unconfirmed_inflight_share_unknown(self):
        metrics, ledger, workers = Metrics(), AcceptedWorkLedger(), WorkerRegistry()
        workers.authorize(1, 'worker', '127.0.0.1')
        relay = SimpleNamespace(fee=None, fee_owner=None, ledger=ledger, metrics=metrics,
                                workers=workers, validate_pow=False)
        metrics._audit = WorkAudit(ledger)
        session = {'id': 1, 'metrics': metrics}
        router = StatefulRouter(relay, None, None, session)
        router.auth['worker'] = 'x'
        entered = asyncio.Event()
        async def pending(*args):
            entered.set()
            await asyncio.Future()
        channel = SimpleNamespace(target='user', closed=False, extranonce1='11', extranonce2_size=4, request=pending)
        router.jobs['job'] = (channel, 'original', [], '11', 4, 2, None)
        task = asyncio.create_task(router.submit(1, ['worker', 'job', '00000000', '65000000', '00000000']))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual((metrics.accepted, metrics.unmatched, session['pending']), (0, 1, 0))
        self.assertEqual(workers.snapshot()['workers'][0]['unknown'], 1)
        self.assertEqual(ledger.user_work, 0)
