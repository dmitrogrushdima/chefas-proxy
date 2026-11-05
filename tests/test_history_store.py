import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from observer import WorkerRegistry
from history_store import HistoryStore


class HistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_background_and_shutdown_checkpoint_restore(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'history.json'
            registry=WorkerRegistry()
            registry.authorize(1,'alice','127.0.0.1')
            registry.record(1,'alice','accepted',difficulty='2.5',rtt_ms=8)
            registry.record(1,'alice','rejected',stale=True)
            registry.first_share(1,'alice',100)
            registry.first_share(1,'alice',200)
            store=HistoryStore(registry,path,interval=.02)
            store.start()
            for _ in range(100):
                if path.exists(): break
                await asyncio.sleep(.01)
            self.assertTrue(path.exists())
            registry.record(1,'alice','accepted',difficulty=1)
            await store.close()
            restored=WorkerRegistry()
            HistoryStore(restored,path).load()
            row=restored.snapshot()['workers'][0]
            self.assertEqual((row['accepted'],row['rejected'],row['stale'],row['status']),(2,1,1,'offline'))
            self.assertEqual(row['notify_to_first_share_ema_ms'],120)
            self.assertEqual(row['notify_to_first_share_samples'],2)
            self.assertEqual(sum(b[1] for b in next(iter(restored.entries.values()))['work'].buckets),3.5)
            self.assertFalse(restored.snapshot()['resets_on_restart'])

    async def test_reboot_clock_rebase_and_downtime_expire_work(self):
        before=[1000.0]
        registry=WorkerRegistry(clock=lambda:before[0])
        registry.authorize(1,'a','127.0.0.1')
        registry.record(1,'a','accepted',difficulty=2)
        state=registry.dump_history(wall_now=20000)
        after=[10.0]
        restored=WorkerRegistry(clock=lambda:after[0])
        restored.restore_history(state,wall_now=21000)
        row=restored.snapshot()['workers'][0]
        self.assertEqual(row['hashrate_windows']['15m']['hashrate_hs'],0)
        self.assertGreater(row['hashrate_windows']['1h']['hashrate_hs'],0)
        expired=WorkerRegistry(clock=lambda:after[0])
        expired.restore_history(state,wall_now=110000)
        self.assertFalse(expired.snapshot()['workers'])

    async def test_corrupt_history_is_not_overwritten_on_load(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'history.json'
            path.write_text('{bad',encoding='utf-8')
            with self.assertRaises(ValueError): HistoryStore(WorkerRegistry(),path).load()
            self.assertEqual(path.read_text(),'{bad')

    async def test_cancelled_background_task_still_saves_final_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'history.json'
            registry = WorkerRegistry()
            registry.authorize(1, 'a', '127.0.0.1')
            registry.record(1, 'a', 'accepted', difficulty=1)
            store = HistoryStore(registry, path)
            store.start()
            store.task.cancel()
            await store.close()
            restored = WorkerRegistry()
            HistoryStore(restored, path).load()
            self.assertEqual(restored.snapshot()['workers'][0]['accepted'], 1)
            # A delayed executor write from the cancelled earlier checkpoint
            # cannot overwrite the newer shutdown generation.
            older = registry.dump_history()
            older['workers'][0]['accepted'] = 0
            store._write(older, 0)
            again = WorkerRegistry()
            HistoryStore(again, path).load()
            self.assertEqual(again.snapshot()['workers'][0]['accepted'], 1)
