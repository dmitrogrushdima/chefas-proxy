import asyncio
import json
import unittest

from dashboard_server import DashboardStream, dashboard_request
from observer import Metrics


class DashboardStreamTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.state = {'registry': {'workers': []}, 'count': 0}
        self.stream = DashboardStream(lambda: self.state, interval=.03, max_clients=1)
        self.server = await asyncio.start_server(lambda r,w: dashboard_request(r,w,Metrics(),stream=self.stream), '127.0.0.1', 0)
        self.port = self.server.sockets[0].getsockname()[1]
        self.writers = []

    async def asyncTearDown(self):
        for writer in self.writers:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
        await self.stream.close()
        self.server.close()
        await self.server.wait_closed()

    async def request(self, path='/api/stream', extra='', method='GET'):
        reader, writer = await asyncio.open_connection('127.0.0.1', self.port)
        self.writers.append(writer)
        writer.write(f'{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n{extra}\r\n'.encode())
        await writer.drain()
        headers = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 2)
        return reader, writer, headers

    async def test_multiple_frames_latest_state_and_client_limit(self):
        reader, writer, headers = await self.request()
        self.assertIn(b'text/event-stream', headers)
        self.assertNotIn(b'Content-Length', headers)
        frame = await asyncio.wait_for(reader.readuntil(b'\n\n'), 2)
        self.assertIn(b'event: snapshot', frame)
        self.state['count'] = 3
        for _ in range(20):
            frame = await asyncio.wait_for(reader.readuntil(b'\n\n'), 2)
            if json.loads(frame.split(b'data: ')[1])['count'] == 3:
                break
        else:
            self.fail("Updated snapshot did not arrive")
        _, _, headers = await self.request()
        self.assertIn(b'503 Service Unavailable', headers)
        self.assertEqual(len(self.stream.queues), 1)
        writer.close()
        await writer.wait_closed()
        for _ in range(100):
            if not self.stream.queues:
                break
            await asyncio.sleep(.01)
        self.assertFalse(self.stream.queues)
        self.assertIsNone(self.stream.producer)
        _, _, headers = await self.request()
        self.assertIn(b'200 OK', headers)

    async def test_snapshot_fallback_and_cross_site_and_post_are_rejected(self):
        reader, _, headers = await self.request('/api/snapshot')
        self.assertIn(b'200 OK', headers)
        self.assertEqual(json.loads(await reader.read()), self.state)
        _, _, headers = await self.request(extra='Origin: https://attacker.invalid\r\n')
        self.assertIn(b'403 Forbidden', headers)
        _, _, headers = await self.request(method='POST')
        self.assertIn(b'403 Forbidden', headers)
        self.assertFalse(self.stream.queues)

    async def test_latest_only_queues_are_bounded_and_snapshot_error_recovers(self):
        queue = asyncio.Queue(maxsize=1)
        self.stream.queues.add(queue)
        self.stream.producer = asyncio.create_task(self.stream._produce())
        await asyncio.sleep(.12)
        self.assertEqual(queue.qsize(), 1)
        self.assertGreater(self.stream.sequence, 1)
        self.state['bad'] = float('nan')
        await asyncio.sleep(.06)
        self.assertIn(b'event: stream-error', queue.get_nowait())
        self.state.pop('bad')
        await asyncio.sleep(.06)
        self.assertIn(b'event: snapshot', queue.get_nowait())
        self.stream.queues.discard(queue)

    async def test_stuck_browser_is_removed_after_bounded_drain(self):
        class SlowWriter:
            def write(self, data):
                pass
            async def drain(self):
                await asyncio.Future()
        stream = DashboardStream(lambda: {}, drain_timeout=.03)
        try:
            with self.assertRaises(asyncio.TimeoutError):
                await stream.serve(asyncio.StreamReader(), SlowWriter())
            self.assertFalse(stream.queues)
            self.assertIsNone(stream.producer)
        finally:
            await stream.close()
