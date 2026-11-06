import asyncio
import json
import socket
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from network_io import (AsyncResolver, WRITE_HIGH, WRITE_HARD, write_bounded, drain_bounded)
from observer import Metrics
from upstream_manager import (open_upstream, UpstreamConnection, UpstreamLoopError,
                              connect_upstream, encode_packet)
from stratum_session import Relay
from tests import test_network_guards as fixtures


class Transport:
    def __init__(self, size=0):
        self.size,self.reading,self.closed=size,True,False
        self.pauses=self.resumes=0
    def get_write_buffer_size(self):return self.size
    def is_reading(self):return self.reading
    def is_closing(self):return self.closed
    def pause_reading(self):self.reading=False;self.pauses+=1
    def resume_reading(self):self.reading=True;self.resumes+=1
    def abort(self):self.closed=True


class EdgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_dns_singleflight_cancel_isolation_cache_and_capacity(self):
        calls=[];release=asyncio.Event();started=asyncio.Event()
        async def lookup(host,port):
            calls.append(host);started.set();await release.wait()
            return [(socket.AF_INET,socket.SOCK_STREAM,6,'',('127.0.0.1',port))]
        resolver=AsyncResolver(lookup=lookup,max_pending=1,capacity=2)
        tasks=[asyncio.create_task(resolver.resolve('pool.test',3333,1)) for _ in range(20)]
        try:
            await asyncio.wait_for(started.wait(),1)
            tasks[0].cancel()
            with self.assertRaises(OSError):await resolver.resolve('other.test',3333,1)
            self.assertEqual(len(calls),1)
            release.set()
            results=await asyncio.gather(*tasks,return_exceptions=True)
            self.assertIsInstance(results[0],asyncio.CancelledError)
            self.assertTrue(all(not isinstance(r,BaseException) for r in results[1:]))
            await resolver.resolve('pool.test',3333,1)
            self.assertEqual(len(calls),1)
            for n in range(3):await resolver.resolve(str(n)+'.test',3333,1)
            self.assertLessEqual(len(resolver.cache),2)
        finally:await resolver.close()

    async def test_hung_dns_times_out_without_stopping_event_loop(self):
        async def hung(host,port):await asyncio.Event().wait()
        resolver=AsyncResolver(lookup=hung,query_timeout=.05)
        ticks=[]
        async def ticker():
            for n in range(10):ticks.append(n);await asyncio.sleep(.01)
        try:
            query=asyncio.create_task(resolver.resolve('hung.test',3333,1))
            await ticker()
            with self.assertRaises(asyncio.TimeoutError):await query
            self.assertEqual(len(ticks),10)
            self.assertFalse(resolver.pending)
            with self.assertRaises(OSError):await resolver.resolve('hung.test',3333,1)
        finally:await resolver.close()

    async def test_numeric_connect_never_uses_system_dns(self):
        async def peer(reader,writer):
            await reader.read();writer.close();await writer.wait_closed()
        server=await asyncio.start_server(peer,'127.0.0.1',0)
        writer=None
        try:
            with patch.object(asyncio.get_running_loop(),'getaddrinfo',side_effect=AssertionError('system DNS used')):
                _,writer=await open_upstream(('127.0.0.1',server.sockets[0].getsockname()[1]),None,2)
        finally:
            if writer:writer.close();await writer.wait_closed()
            server.close();await server.wait_closed()

    async def test_unpaused_real_writer_drain_needs_no_child_task(self):
        async def peer(reader,writer):
            try:await reader.read()
            finally:writer.close();await writer.wait_closed()
        server=await asyncio.start_server(peer,'127.0.0.1',0)
        reader,writer=await asyncio.open_connection('127.0.0.1',server.sockets[0].getsockname()[1])
        try:
            writer.write(b'valid-small-share\n')
            with patch('network_io.asyncio.ensure_future',side_effect=AssertionError('extra task on hot path')):
                await drain_bounded(writer,1)
        finally:
            writer.close();await writer.wait_closed()
            server.close();await server.wait_closed()

    async def test_ttl_zero_dns_rebinding_cannot_bypass_self_route_guard(self):
        accepted=[]
        async def peer(reader,writer):
            accepted.append(writer)
            try:await reader.read()
            finally:writer.close();await writer.wait_closed()
        server=await asyncio.start_server(peer,'127.0.0.1',0)
        port=server.sockets[0].getsockname()[1]
        calls=[]
        async def lookup(host,port):
            ip='203.0.113.1' if len(calls) % 2 == 0 else '127.0.0.1'
            calls.append(ip)
            return [(socket.AF_INET,socket.SOCK_STREAM,6,'',(ip,port))],0
        resolver=AsyncResolver(lookup=lookup)
        relay=Relay('pool.test',port,timeout=1)
        await relay.resolver.close()
        relay.resolver=resolver
        relay.listeners=[(socket.AF_INET,('127.0.0.1',port))]
        try:
            # Preflight sees a public address, actual dial sees our listener.
            self.assertFalse(await relay.is_self_route(('pool.test',port)))
            with self.assertRaises(UpstreamLoopError):
                await open_upstream(('pool.test',port),None,1,resolver=resolver,
                                    address_guard=relay.is_self_route)
            metrics=Metrics()
            result=await connect_upstream(('pool.test',port),None,None,1,
                relay.is_self_route,metrics,resolver=resolver)
            self.assertEqual(result,(None,None,None))
            self.assertEqual(metrics.blocked_loops,1)
            self.assertEqual(metrics.upstream_failures,1)
            self.assertEqual(calls,['203.0.113.1','127.0.0.1']*2)
            self.assertFalse(accepted)
        finally:
            await resolver.close()
            server.close();await server.wait_closed()

    async def test_backpressure_pauses_resumes_and_hard_caps_without_write(self):
        source=SimpleNamespace(transport=Transport())
        target=SimpleNamespace(transport=Transport(WRITE_HIGH),close=lambda:None)
        metrics=Metrics()
        release=asyncio.Event()
        target.drain=release.wait
        task=asyncio.create_task(drain_bounded(target,1,source,metrics=metrics))
        await asyncio.sleep(0)
        self.assertFalse(source.transport.reading)
        release.set();await task
        self.assertTrue(source.transport.reading)
        self.assertEqual(metrics.backpressure_pauses,1)
        target.transport.size=WRITE_HARD
        target.write=lambda data:self.fail('oversized write escaped')
        with self.assertRaises(ConnectionError):write_bounded(target,b'x',metrics)
        self.assertTrue(target.transport.closed)

    async def test_drain_timeout_aborts_only_the_stalled_writer(self):
        transport=Transport(WRITE_HIGH);source=SimpleNamespace(transport=Transport())
        target=SimpleNamespace(transport=transport,close=lambda:None,drain=asyncio.Event().wait)
        metrics=Metrics()
        with self.assertRaises(asyncio.TimeoutError):await drain_bounded(target,.05,source,metrics=metrics)
        self.assertTrue(transport.closed)
        self.assertTrue(source.transport.reading)
        self.assertEqual(metrics.write_timeouts,1)

    async def test_parallel_drains_keep_source_paused_until_last_finishes(self):
        for first in (0,1):
            with self.subTest(first=first):
                source=SimpleNamespace(transport=Transport())
                releases=[asyncio.Event(),asyncio.Event()]
                targets=[SimpleNamespace(transport=Transport(WRITE_HIGH),close=lambda:None,
                                         drain=release.wait) for release in releases]
                tasks=[asyncio.create_task(drain_bounded(target,5,source)) for target in targets]
                try:
                    await asyncio.sleep(0)
                    self.assertEqual(source._chefas_proxy_read_pressure['users'],2)
                    releases[first].set();await tasks[first]
                    self.assertFalse(source.transport.reading)
                    self.assertEqual(source.transport.resumes,0)
                    releases[1-first].set();await tasks[1-first]
                    self.assertTrue(source.transport.reading)
                    self.assertEqual(source.transport.pauses,1)
                    self.assertEqual(source.transport.resumes,1)
                    self.assertEqual(source._chefas_proxy_read_pressure['users'],0)
                finally:
                    for task in tasks:task.cancel()
                    await asyncio.gather(*tasks,return_exceptions=True)

    async def test_cancelled_parallel_drain_releases_only_its_own_pause(self):
        for cancelled in (0,1):
            with self.subTest(cancelled=cancelled):
                source=SimpleNamespace(transport=Transport())
                releases=[asyncio.Event(),asyncio.Event()]
                targets=[SimpleNamespace(transport=Transport(WRITE_HIGH),close=lambda:None,
                                         drain=release.wait) for release in releases]
                tasks=[asyncio.create_task(drain_bounded(target,5,source)) for target in targets]
                try:
                    await asyncio.sleep(0)
                    tasks[cancelled].cancel()
                    with self.assertRaises(asyncio.CancelledError):await tasks[cancelled]
                    self.assertFalse(source.transport.reading)
                    self.assertEqual(source._chefas_proxy_read_pressure['users'],1)
                    releases[1-cancelled].set();await tasks[1-cancelled]
                    self.assertTrue(source.transport.reading)
                    self.assertEqual(source.transport.resumes,1)
                finally:
                    for task in tasks:task.cancel()
                    await asyncio.gather(*tasks,return_exceptions=True)

    async def test_drain_does_not_resume_streamreader_or_external_pause(self):
        for initially_paused in (False,True):
            with self.subTest(initially_paused=initially_paused):
                source=SimpleNamespace(transport=Transport())
                reader=SimpleNamespace(_paused=initially_paused)
                source.transport.reading=not initially_paused
                release=asyncio.Event()
                target=SimpleNamespace(transport=Transport(WRITE_HIGH),close=lambda:None,
                                       drain=release.wait)
                task=asyncio.create_task(drain_bounded(target,5,source,reader))
                try:
                    await asyncio.sleep(0)
                    reader._paused=True  # StreamReader independently needs its pause.
                    release.set();await task
                    self.assertFalse(source.transport.reading)
                    self.assertEqual(source.transport.resumes,0)
                    self.assertEqual(source._chefas_proxy_read_pressure['users'],0)
                finally:
                    task.cancel();await asyncio.gather(task,return_exceptions=True)

    async def test_pool_rpc_response_bypasses_blocked_asic_notification_writer(self):
        router,_=self.router_channel()
        async def pool(reader,writer):
            try:
                request=json.loads(await reader.readline())
                for n in range(100):
                    job=['job-'+str(n),'00'*32,'','',[],'20000000','207fffff','65000000',True]
                    writer.write(encode_packet({'method':'mining.notify','params':job}))
                writer.write(encode_packet({'id':request['id'],'result':True,'error':None}))
                await writer.drain()
                await reader.read()
            finally:
                writer.close()
                await writer.wait_closed()
        server=await asyncio.start_server(pool,'127.0.0.1',0)
        reader,writer=await asyncio.open_connection('127.0.0.1',server.sockets[0].getsockname()[1])
        async def disconnected(ch):pass
        ch=UpstreamConnection(reader,writer,'user',router.notification,disconnected,timeout=1)
        ch.authorized=ch.subscribed=True
        ch.extranonce1='11111111';ch.extranonce2_size=4;ch.difficulty=1
        router.current=ch
        await router.write_lock.acquire()
        dispatcher=asyncio.create_task(router.dispatch_notifications())
        try:
            response=await asyncio.wait_for(ch.request('mining.submit',[]),1)
            self.assertIs(response['result'],True)
            self.assertFalse(router.writer.wire)
            self.assertLessEqual(int(router.urgent_job is not None)+int(router.normal_job is not None),2)
        finally:
            router.stopping=True
            router.write_lock.release()
            dispatcher.cancel()
            await asyncio.gather(dispatcher,return_exceptions=True)
            await ch.close()
            server.close();await server.wait_closed()

    def router_channel(self):
        fixture=fixtures.NetworkGuardsTests()
        router=fixture.router();ch=fixture.channel('user')
        ch.authorized=ch.subscribed=True
        ch.extranonce2_size=4;ch.difficulty=1
        router.current=ch
        router.auth['worker']='x'
        return router,ch

    async def test_clean_jobs_coalesce_bound_queue_and_preempt_queued_fee_switch(self):
        router,ch=self.router_channel()
        fee=fixtures.NetworkGuardsTests().channel('fee')
        await router.write_lock.acquire()
        switching=asyncio.create_task(router.activate(fee))
        await asyncio.sleep(0)
        for n in range(100):
            params=list(ch.job);params[0]=str(n);params[8]=True
            await router.notification(ch,{'method':'mining.notify','params':params})
        self.assertIsNone(router.normal_job)
        self.assertEqual(router.urgent_job[1][0][0],'99')
        router.write_lock.release()
        self.assertFalse(await switching)
        self.assertIs(router.current,ch)
        task=asyncio.create_task(router.dispatch_notifications())
        try:
            for _ in range(30):
                if router.jobs:break
                await asyncio.sleep(.01)
            self.assertEqual(len(router.jobs),1)
            self.assertEqual(next(iter(router.jobs.values()))[1],'99')
            packets=[json.loads(line) for line in router.writer.wire.splitlines()]
            self.assertEqual([p['method'] for p in packets],['mining.set_difficulty','mining.notify'])
            self.assertTrue(packets[-1]['params'][-1])
        finally:
            router.stopping=True
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)
