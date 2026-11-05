import asyncio
import gc
import random
import time
import unittest
import weakref
from types import SimpleNamespace
from unittest.mock import AsyncMock

from devfee_scheduler import FeeJitter, FeeConnectionGate
from router import StatefulRouter
from stratum_session import Relay


class Channel:
    closed = False
    target = 'user'
    job = ['job','00'*32,'','',[],'20000000','207fffff','65000000',False]
    job_extranonce = '11111111'
    job_extra_size = 4
    job_difficulty = 1


class StabilityTests(unittest.IsolatedAsyncioTestCase):
    def router(self):
        relay = Relay('localhost',3333)
        session = {'id':1,'worker':'worker','observer':SimpleNamespace(protocol={})}
        return StatefulRouter(relay,asyncio.StreamReader(),None,session)

    def test_jitter_is_bounded_independent_and_monotonic(self):
        now = [1000.0]
        rng = random.Random(42)
        timers = [FeeJitter(180,clock=lambda:now[0],uniform=rng.uniform) for _ in range(400)]
        delays = [j.remaining() for j in timers]
        self.assertTrue(all(0 <= delay <= 180 for delay in delays))
        self.assertGreater(len(set(int(delay) for delay in delays)),140)
        now[0] += 181
        self.assertTrue(all(j.remaining() == 0 for j in timers))
        timers[0].defer()
        self.assertGreater(timers[0].remaining(),0)
        self.assertEqual(FeeJitter(0).remaining(),0)

    async def test_connection_gate_spaces_concurrent_attempts_and_survives_cancel(self):
        gate = FeeConnectionGate(.02)
        attempts = []
        async def attempt():
            attempts.append(await gate.wait())
        await gate.wait()
        cancelled = asyncio.create_task(attempt())
        await asyncio.sleep(0)
        cancelled.cancel()
        await asyncio.gather(cancelled,return_exceptions=True)
        await asyncio.wait_for(asyncio.gather(*(attempt() for _ in range(8))),3)
        self.assertEqual(len(attempts),8)
        self.assertTrue(all(b-a >= .018 for a,b in zip(attempts,attempts[1:])))

    async def test_registry_remains_bounded_and_ttl_releases_channel_references(self):
        router = self.router()
        channel = Channel()
        ref = weakref.ref(channel)
        for _ in range(2000):
            packet = router.make_job(channel,False)
            ident = packet['params'][0]
            router.duplicates[(ident,)] = (ident,time.monotonic())
        self.assertLessEqual(len(router.jobs),128)
        self.assertEqual(set(router.job_times),set(router.jobs))
        self.assertEqual(set(router.job_layouts),set(router.jobs))
        self.assertLessEqual(len(router.duplicates),128)
        del channel
        self.assertIsNotNone(ref())
        router.prune_jobs(time.monotonic()+901)
        gc.collect()
        self.assertFalse(router.jobs)
        self.assertFalse(router.job_times)
        self.assertFalse(router.job_layouts)
        self.assertFalse(router.duplicates)
        self.assertIsNone(ref())

    async def test_background_collector_expires_idle_registry(self):
        router = self.router()
        router.relay.job_gc_interval = .01
        old = router.make_job(Channel(),False)['params'][0]
        router.job_times[old] = (time.monotonic()-901,set())
        fresh = router.make_job(Channel(),False)['params'][0]
        # Inject old state after normal publication's opportunistic cleanup.
        router.jobs[old] = router.jobs[fresh]
        router.job_times[old] = (time.monotonic()-901,set())
        router.duplicates[('old',)] = (old,time.monotonic())
        task = asyncio.create_task(router.collect_jobs())
        try:
            await asyncio.sleep(.05)
            self.assertNotIn(old,router.jobs)
            self.assertNotIn(old,router.job_times)
            self.assertFalse(router.duplicates)
            self.assertIn(fresh,router.jobs)
        finally:
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)

    async def test_planned_jitter_does_not_start_outage_watchdog(self):
        router = self.router()
        router.relay.require_devfee = True
        router.auth['worker'] = 'x'
        permitted = asyncio.Event()
        router.relay.fee_connection_gate = SimpleNamespace(wait=lambda delay:permitted.wait())
        router.connect = AsyncMock(side_effect=ConnectionError('fixture'))
        task = asyncio.create_task(router.prepare('fee',('localhost',3334),fee=True))
        try:
            await asyncio.sleep(0)
            self.assertFalse(router.fee_policy_started)
            router.connect.assert_not_called()
            permitted.set()
            with self.assertRaises(ConnectionError):
                await task
            self.assertTrue(router.fee_policy_started)
        finally:
            task.cancel()
            for child in router.tasks:
                child.cancel()
            await asyncio.gather(task,*router.tasks,return_exceptions=True)

    def test_invalid_timer_configuration_is_rejected(self):
        for value in (-1,float('nan'),float('inf'),3601):
            with self.assertRaises(ValueError):
                FeeJitter(value)
        for value in (-1,float('nan'),61):
            with self.assertRaises(ValueError):
                FeeConnectionGate(value)
