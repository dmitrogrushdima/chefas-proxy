"""Real StreamWriter checks for the allocation-free, nonblocking drain path."""
import asyncio
import unittest
from unittest.mock import patch

from network_io import drain_bounded
from observer import Metrics


class NativeDrainTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.peers = set()

        async def peer(reader, writer):
            task = asyncio.current_task()
            self.peers.add(task)
            try:
                await reader.read()
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except ConnectionError:
                    pass
                self.peers.discard(task)

        self.server = await asyncio.start_server(peer, '127.0.0.1', 0)
        self.reader, self.writer = await asyncio.open_connection(
            '127.0.0.1', self.server.sockets[0].getsockname()[1])
        self.assertIs(type(self.writer), asyncio.StreamWriter)
        self.assertIs(type(self.writer._protocol), asyncio.StreamReaderProtocol)
        self.assertIs(self.writer._protocol._paused, False)

    async def asyncTearDown(self):
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except ConnectionError:
            pass
        self.server.close()
        await self.server.wait_closed()
        if self.peers:
            await asyncio.gather(*self.peers, return_exceptions=True)

    async def test_unpaused_native_drain_allocates_no_task(self):
        self.writer.write(b'small Stratum packet\n')
        with patch('network_io.asyncio.ensure_future',
                   side_effect=AssertionError('native fast drain allocated a task')):
            await drain_bounded(self.writer, .01)
        self.assertFalse(self.writer.is_closing())

    async def test_unpaused_native_drain_preserves_reader_exception(self):
        error = ConnectionResetError('synthetic reader failure')
        self.reader.set_exception(error)
        with patch('network_io.asyncio.ensure_future',
                   side_effect=AssertionError('reader error bypassed fast path')):
            with self.assertRaises(ConnectionResetError) as raised:
                await drain_bounded(self.writer, .01)
        self.assertIs(raised.exception, error)

    async def test_unpaused_native_drain_preserves_protocol_connection_loss(self):
        self.writer._protocol._connection_lost = True
        with patch('network_io.asyncio.ensure_future',
                   side_effect=AssertionError('protocol error bypassed fast path')):
            with self.assertRaises(ConnectionResetError):
                await drain_bounded(self.writer, .01)

    async def test_paused_native_writer_uses_timeout_and_aborts_transport(self):
        # Invoke the real flow-control callback, as a full transport buffer does.
        self.writer._protocol.pause_writing()
        metrics = Metrics()
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(drain_bounded(self.writer, .01, metrics=metrics), 1)
        self.assertTrue(self.writer.is_closing())
        self.assertEqual(metrics.write_timeouts, 1)

    async def test_monkeypatched_instance_drain_keeps_timeout(self):
        entered, cancelled = asyncio.Event(), asyncio.Event()

        async def blocking_drain():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        self.writer.drain = blocking_drain
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(drain_bounded(self.writer, .01), 1)
        self.assertTrue(entered.is_set())
        self.assertTrue(cancelled.is_set())
        self.assertTrue(self.writer.is_closing())

    async def test_monkeypatched_instance_drain_propagates_outer_cancellation(self):
        entered, cancelled = asyncio.Event(), asyncio.Event()

        async def blocking_drain():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        self.writer.drain = blocking_drain
        task = asyncio.create_task(drain_bounded(self.writer, 5))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(cancelled.is_set())
            self.assertFalse(self.writer.is_closing())
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_monkeypatched_protocol_helper_keeps_timeout(self):
        entered = asyncio.Event()

        async def blocking_helper():
            entered.set()
            await asyncio.Event().wait()

        self.writer._protocol._drain_helper = blocking_helper
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(drain_bounded(self.writer, .01), 1)
        self.assertTrue(entered.is_set())
        self.assertTrue(self.writer.is_closing())
