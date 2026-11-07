import asyncio
import json
import unittest

from aggregation import AggregationKey, InsufficientNonceSpace, NoncePartitioner
from upstream_manager import SharedUpstreamManager, UpstreamConnection


class SharedUpstreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_miners_share_authenticated_socket_and_unique_prefixes(self):
        connections = []
        async def pool(reader, writer):
            connections.append(writer)
            try:
                while line := await reader.readline():
                    request = json.loads(line)
                    result = [[], 'aabb', 4] if request['method'] == 'mining.subscribe' else True
                    writer.write((json.dumps({'id': request['id'], 'result': result, 'error': None}) + '\n').encode())
                    await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
        server = await asyncio.start_server(pool, '127.0.0.1', 0)
        self.addAsyncCleanup(server.wait_closed)
        self.addCleanup(server.close)
        port = server.sockets[0].getsockname()[1]
        slow_open_started = asyncio.Event()
        resume_slow_open = asyncio.Event()
        async def noop(*args):
            pass
        async def factory(key):
            if key.worker == 'slow.worker':
                slow_open_started.set()
                await resume_slow_open.wait()
            reader, writer = await asyncio.open_connection(key.host, key.port)
            connection = UpstreamConnection(reader, writer, 'user', noop, noop)
            result = await connection.request('mining.subscribe', ['test'])
            connection.extranonce1, connection.extranonce2_size = result['result'][1:]
            connection.subscribed = True
            result = await connection.request('mining.authorize', [key.worker, key.password])
            connection.authorized = result['result'] is True
            return connection, NoncePartitioner(connection.extranonce1, connection.extranonce2_size, 3, maximum_workers=2)
        manager = SharedUpstreamManager(factory, max_groups=4)
        self.addAsyncCleanup(manager.close)
        key = AggregationKey('127.0.0.1', port, False, 'farm.worker', 'x')
        (group1, first), (group2, second) = await asyncio.gather(manager.acquire(key, 'one'), manager.acquire(key, 'two'))
        self.assertIs(group1, group2)
        self.assertEqual(len(connections), 1)
        self.assertNotEqual(first.prefix, second.prefix)
        self.assertEqual(first.downstream_extranonce1 + '010203', 'aabb' + first.upstream_extranonce2('010203'))
        received1, received2 = asyncio.Queue(), asyncio.Queue()
        async def failure():
            self.fail('subscriber failed')
        group1.subscribe('one', received1.put, failure)
        group2.subscribe('two', received2.put, failure)
        notice = {'id': None, 'method': 'mining.notify', 'params': ['job']}
        connections[0].write((json.dumps(notice) + '\n').encode())
        await connections[0].drain()
        self.assertEqual(await asyncio.wait_for(received1.get(), 2), notice)
        self.assertEqual(await asyncio.wait_for(received2.get(), 2), notice)
        group1.release('one')
        notice['params'] = ['next']
        connections[0].write((json.dumps(notice) + '\n').encode())
        await connections[0].drain()
        self.assertEqual(await asyncio.wait_for(received2.get(), 2), notice)
        self.assertTrue(received1.empty())
        self.assertFalse(group2.connection.closed)
        response = await group2.connection.request('mining.authorize', [key.worker, key.password])
        self.assertTrue(response['result'])
        _, third = await manager.acquire(key, 'three')
        self.assertNotEqual(first.prefix, third.prefix)
        other = AggregationKey('127.0.0.1', port, False, 'another.worker', 'x')
        other_group, _ = await manager.acquire(other, 'four')
        self.assertEqual(len(connections), 2)

        slow = AggregationKey('127.0.0.1', port, False, 'slow.worker', 'x')
        pending = asyncio.create_task(manager.acquire(slow, 'slow'))
        try:
            await asyncio.wait_for(slow_open_started.wait(), 2)
            joined, _ = await asyncio.wait_for(manager.acquire(other, 'five'), 2)
            self.assertIs(joined, other_group)
            await asyncio.wait_for(manager.release(other_group, 'four'), 2)
            self.assertFalse(other_group.connection.closed)
            self.assertEqual(len(connections), 2)

            healthy = AggregationKey('127.0.0.1', port, False, 'healthy.worker', 'x')
            await asyncio.wait_for(manager.acquire(healthy, 'healthy'), 2)
            self.assertEqual(len(connections), 3)
            overflow = AggregationKey('127.0.0.1', port, False, 'overflow.worker', 'x')
            with self.assertRaises(InsufficientNonceSpace):
                await asyncio.wait_for(manager.acquire(overflow, 'overflow'), 2)
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)

        resume_slow_open.set()
        await asyncio.wait_for(manager.acquire(slow, 'retry'), 2)
        self.assertEqual(len(connections), 4)
