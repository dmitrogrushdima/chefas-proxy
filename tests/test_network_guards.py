"""Deterministic races at the job/extranonce boundary, without timing guesses."""
import asyncio
import json
import socket
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

from router import StatefulRouter
from stratum_session import Relay, enable_tcp_keepalive


class Writer:
    def __init__(self):
        self.wire = bytearray()

    def write(self, data):
        self.wire.extend(data)

    async def drain(self):
        pass


class NetworkGuardsTests(unittest.IsolatedAsyncioTestCase):
    def router(self):
        relay = Relay('localhost',3333)
        session = {'id':1,'worker':'worker','observer':SimpleNamespace(protocol={})}
        return StatefulRouter(relay,asyncio.StreamReader(),Writer(),session)

    def channel(self, target='fee', difficulty=Decimal('0.000000000123456789')):
        ready = asyncio.Event()
        ready.set()
        return SimpleNamespace(closed=False, ready=ready, target=target, extranonce1='22222222',
            job_extranonce='22222222',job_extra_size=4,job_difficulty=difficulty,
            job=['pool-job','00'*32,'','',[],'20000000','207fffff','65000000',False])

    async def test_queued_old_pool_packets_are_dropped_after_context_switch(self):
        router = self.router()
        old = self.channel('user')
        router.current = old
        await router.write_lock.acquire()
        publish = asyncio.create_task(router.publish(old,False))
        difficulty = asyncio.create_task(router.send_current(old,
            {'id':None,'method':'mining.set_difficulty','params':[999]}))
        await asyncio.sleep(0)  # Both tasks are queued on the lock.
        router.current = self.channel()
        router.write_lock.release()
        await asyncio.gather(publish,difficulty)
        self.assertEqual(router.writer.wire,b'')
        self.assertFalse(router.jobs)

    async def test_hot_switch_sends_exact_job_difficulty_and_retains_old_mapping(self):
        router = self.router()
        user = self.channel('user',Decimal('8192'))
        router.current = user
        await router.publish(user,False)
        old_id = next(iter(router.jobs))
        router.writer.wire.clear()
        fee = self.channel()
        # A pending vardiff threshold must never replace the cached job threshold.
        fee.difficulty = Decimal('4096')
        self.assertTrue(await router.activate(fee))
        packets = [json.loads(line,parse_float=Decimal) for line in router.writer.wire.splitlines()]
        self.assertEqual([p['method'] for p in packets],
            ['mining.set_extranonce','mining.set_difficulty','mining.notify'])
        self.assertEqual(packets[1]['params'],[fee.job_difficulty])
        self.assertIs(packets[2]['params'][-1],True)
        self.assertIs(router.jobs[old_id][0],user)
        self.assertEqual(router.jobs[old_id][5],Decimal('8192'))
        self.assertEqual(router.jobs[packets[2]['params'][0]][5],fee.job_difficulty)

    async def test_tcp_keepalive_is_enabled_and_unsupported_tuning_is_tolerated(self):
        sock = Mock()
        def setsockopt(level,option,value):
            if level != socket.SOL_SOCKET:
                raise OSError('unsupported tuning')
        sock.setsockopt.side_effect = setsockopt
        writer = Mock()
        writer.get_extra_info.return_value = sock
        self.assertTrue(enable_tcp_keepalive(writer))
        sock.setsockopt.assert_any_call(socket.SOL_SOCKET,socket.SO_KEEPALIVE,1)
        writer.get_extra_info.return_value = None
        self.assertFalse(enable_tcp_keepalive(writer))
