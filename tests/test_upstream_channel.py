"""Framing and lifecycle tests for future stateful upstream channels."""
import asyncio
import json
import unittest
from decimal import Decimal

from upstream_manager import ProtocolError, UpstreamConnection, read_packet, encode_packet


class FramingTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_upstream_handles_glued_responses_and_large_fragmented_notify(self):
        notifications = []
        async def pool(reader, writer):
            try:
                first = json.loads(await reader.readline())
                second = json.loads(await reader.readline())
                packets = [dict(id=first['id'],result=True,error=None),
                    dict(id=None,method='mining.notify',params=['x'*10000]),
                    dict(id=second['id'],result=42,error=None)]
                wire = b''.join(json.dumps(p).encode() for p in packets)
                for offset in range(0,len(wire),137):
                    writer.write(wire[offset:offset+137])
                    await writer.drain()
                await reader.read()
            finally:
                writer.close()
                await writer.wait_closed()
        server = await asyncio.start_server(pool,'127.0.0.1',0)
        reader, writer = await asyncio.open_connection('127.0.0.1',server.sockets[0].getsockname()[1])
        async def notify(channel, packet):
            notifications.append(packet)
        async def disconnected(channel):
            pass
        channel = UpstreamConnection(reader,writer,'user',notify,disconnected,timeout=2)
        try:
            replies = await asyncio.gather(channel.request('mining.subscribe',[]),
                                           channel.request('mining.authorize',['w','x']))
            self.assertEqual([p['result'] for p in replies],[True,42])
            self.assertEqual(notifications[0]['params'],['x'*10000])
        finally:
            await channel.close()
            server.close()
            await server.wait_closed()

    async def test_truncated_frame_is_not_executed(self):
        reader = asyncio.StreamReader()
        reader.feed_data(b'{"id":1')
        reader.feed_eof()
        with self.assertRaises(ProtocolError):
            await read_packet(reader)

    async def test_oversized_frame_is_rejected(self):
        reader = asyncio.StreamReader(limit=65536)
        reader.feed_data(b'x' * 65537 + b'\n')
        with self.assertRaises(ProtocolError):
            await read_packet(reader)

    async def test_decimal_difficulty_round_trips_exactly(self):
        reader = asyncio.StreamReader()
        reader.feed_data(encode_packet({'difficulty': Decimal('0.000000001234567890123456789')}))
        self.assertEqual((await read_packet(reader))['difficulty'], Decimal('0.000000001234567890123456789'))

    async def test_upstream_disconnect_releases_pending_request(self):
        async def pool(reader, writer):
            await reader.readline()
            writer.close()
            await writer.wait_closed()
        server = await asyncio.start_server(pool, '127.0.0.1', 0)
        reader, writer = await asyncio.open_connection('127.0.0.1', server.sockets[0].getsockname()[1])
        disconnected = asyncio.Event()
        async def notify(channel, packet):
            pass
        async def close(channel):
            disconnected.set()
        channel = UpstreamConnection(reader, writer, 'user', notify, close, timeout=1)
        try:
            with self.assertRaises(ConnectionError):
                await channel.request('mining.subscribe', [])
            await asyncio.wait_for(disconnected.wait(), 1)
            self.assertFalse(channel.pending)
        finally:
            await channel.close()
            server.close()
            await server.wait_closed()
