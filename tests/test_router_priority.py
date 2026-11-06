"""Pool context must still be usable when the downstream lock is acquired."""
import asyncio
import unittest

from tests import test_network_guards as fixtures


class RouterPriorityTests(unittest.IsolatedAsyncioTestCase):
    async def test_clean_already_queued_prevents_fee_entry(self):
        fixture=fixtures.NetworkGuardsTests()
        router=fixture.router();user=fixture.channel('user');fee=fixture.channel('fee')
        user.authorized=user.subscribed=True
        user.difficulty=user.job_difficulty
        router.current=user
        await router.notification(user,{'method':'mining.notify','params':user.job[:8]+[True]})
        self.assertFalse(await router.activate(fee))
        self.assertIs(router.current,user)
        self.assertFalse(router.writer.wire)

    async def test_clean_dispatch_in_progress_prevents_fee_entry(self):
        fixture=fixtures.NetworkGuardsTests()
        router=fixture.router();user=fixture.channel('user');fee=fixture.channel('fee')
        router.current=user
        router.urgent_inflight=(user,None)
        self.assertFalse(await router.activate(fee))
        self.assertIs(router.current,user)

    async def test_dead_or_reset_upstream_cannot_commit_after_writer_lock_wait(self):
        for mutation in ('closed','extranonce','job'):
            with self.subTest(mutation=mutation):
                fixture=fixtures.NetworkGuardsTests()
                router=fixture.router();user=fixture.channel('user');fee=fixture.channel('fee')
                router.current=user
                await router.write_lock.acquire()
                task=asyncio.create_task(router.activate(fee))
                await asyncio.sleep(0)
                if mutation=='closed':fee.closed=True
                elif mutation=='extranonce':fee.extranonce1='33333333'
                else:fee.job=None
                router.write_lock.release()
                self.assertFalse(await task)
                self.assertIs(router.current,user)
                self.assertFalse(router.writer.wire)
