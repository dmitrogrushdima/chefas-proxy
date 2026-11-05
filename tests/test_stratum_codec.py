import asyncio
import json
import random
import unittest
from unittest.mock import patch
from stratum_codec import StratumDecoder, PacketReader, ProtocolError


def request(ident=1, method='mining.subscribe', params=None):
    return json.dumps({'id':ident,'method':method,'params':params or []},ensure_ascii=False,separators=(',',':')).encode()


class StratumCodecTests(unittest.IsolatedAsyncioTestCase):
    async def test_coalesced_objects_and_arbitrary_fragmentation(self):
        source=b'\r\n'+request(params=['firmware {x} [y] \\" 中文 🚀'])+request(2,'mining.configure')+b'\n'+request(3,'mining.authorize',['worker','x'])
        expected=[]
        for seed in range(20):
            rng=random.Random(seed)
            decoder=StratumDecoder(role='miner')
            packets=[]; offset=0
            while offset<len(source):
                size=rng.randint(1,13)
                packets.extend(decoder.feed(source[offset:offset+size]));offset+=size
            decoder.eof()
            self.assertEqual([p['id'] for p in packets],[1,2,3])
            if expected: self.assertEqual(packets,expected)
            expected=packets

    async def test_json_loads_is_called_once_not_per_byte(self):
        decoder=StratumDecoder(role='miner')
        payload=request(params=['a'*2000])
        original=json.loads
        with patch('stratum_codec.json.loads', wraps=original) as parse:
            for byte in payload: decoder.feed(bytes([byte]))
            self.assertEqual(parse.call_count,1)

    async def test_scanners_invalid_utf8_duplicates_numbers_and_depth_drop(self):
        bad=[b'GET / HTTP/1.1\r\n',b'\x16\x03\x01',b'{"id":1,"method":[]}',
             b'{"id":1,"method":"mining.subscribe","method":"mining.submit"}',
             request(method='eth_submitLogin'), b'{"x":"\xff"}',b'{"x":"\\ud800"}',
             b'{"x":NaN}',b'{"x":1e999999999}',b'{"x":'+b'1'*100+b'}',
             b'{"x":'+b'['*64+b'0'+b']'*64+b'}',b'{"x":"bad\nstring"}',b'{"x":"\\uZZZZ"}']
        for payload in bad:
            with self.subTest(payload=payload[:50]):
                decoder=StratumDecoder(role='miner')
                with self.assertRaises(ProtocolError): decoder.feed(payload)
                self.assertEqual(len(decoder.buffer),0)
                with self.assertRaises(ProtocolError): decoder.feed(request())

    async def test_limit_applies_per_object_not_entire_tcp_burst(self):
        decoder=StratumDecoder(role='miner')
        self.assertEqual(len(decoder.feed(b''.join(request(i) for i in range(200)))),200)
        with self.assertRaises(ProtocolError): decoder.feed(request(params=['x'*4096]))
        self.assertFalse(decoder.buffer)
        with self.assertRaises(ProtocolError): StratumDecoder().feed(b' '*4097)

    async def test_pool_large_notify_has_separate_budget(self):
        packet={'id':None,'method':'mining.notify','params':['job','00'*32,'ab'*5000,'',[],'20000000','1d00ffff','65000000',True]}
        payload=json.dumps(packet).encode()
        self.assertGreater(len(payload),4096)
        self.assertEqual(StratumDecoder(65536,role='pool').feed(payload),[packet])

    async def test_complete_without_newline_is_valid_partial_eof_is_not(self):
        reader=asyncio.StreamReader()
        reader.feed_data(request()+request(2));reader.feed_eof()
        packets=PacketReader(reader,role='miner')
        self.assertEqual((await packets.read_packet())['id'],1)
        self.assertEqual((await packets.read_packet())['id'],2)
        self.assertIsNone(await packets.read_packet())
        reader=asyncio.StreamReader();reader.feed_data(b'{"id":');reader.feed_eof()
        with self.assertRaises(ProtocolError): await PacketReader(reader).read_packet()
