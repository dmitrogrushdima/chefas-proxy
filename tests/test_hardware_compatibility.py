import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from hardware_compatibility import (ClientCapabilities, CompatibilityRegistry,
                                    negotiated_mask, upstream_config, validate_config, merge_config)
from lab.pool import build_header, find_nonce
from pow_validator import build_stratum_header
from lab.pool import COINB1, COINB2, NTIME, NBITS, PREVHASH, VERSION


class HardwareCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    def test_repeated_configure_cannot_accumulate_unbounded_state(self):
        config = None
        for n in range(32):
            config = merge_config(config,[[str(n)],{}])
        with self.assertRaises(ValueError):
            merge_config(config,[['overflow'],{}])
        self.assertEqual(len(config[0]),32)
        with self.assertRaises(ValueError):
            merge_config([['info'],{'a':'x'*2500}],[['info'],{'b':'x'*2500}])
        self.assertEqual(len(merge_config(None,[['info'],{'a':1}])[0]),1)
    def test_config_masks_are_exact_and_minimum_bits_can_degrade(self):
        params = [['version-rolling'],{'version-rolling.mask':'1fffe000','version-rolling.min-bit-count':16}]
        mask = negotiated_mask(params,{'result':{'version-rolling':True,'version-rolling.mask':'00002000'},'error':None})
        self.assertEqual(mask,'00002000')
        fee = upstream_config(params,mask)
        self.assertEqual(fee[1]['version-rolling.mask'],mask)
        self.assertEqual(params[1]['version-rolling.mask'],'1fffe000')
        with self.assertRaises(ValueError):
            negotiated_mask(fee,{'result':{'version-rolling':True,'version-rolling.mask':'1fffe000'}})
        for bad in ('+0000001','zzzzzzzz','1','0000000 '):
            with self.assertRaises(ValueError):
                validate_config([['version-rolling'],{'version-rolling.mask':bad}])

    def test_extranonce_translation_preserves_identical_bitcoin_header(self):
        caps = ClientCapabilities(maximum=2)
        extra,size,prefix = caps.layout('11111111',4)
        job = ['job',PREVHASH,COINB1,COINB2,[],VERSION,NBITS,NTIME,True]
        downstream_ex2 = 'aabb'
        upstream_ex2 = prefix + downstream_ex2
        self.assertEqual((extra,size,prefix),('111111110000',2,'0000'))
        self.assertEqual(build_stratum_header(job,extra,downstream_ex2,NTIME,'00000000'),
                         build_stratum_header(job,'11111111',upstream_ex2,NTIME,'00000000'))
        first = caps.subscription([[], '11111111',4])
        self.assertEqual(first[2],2)
        self.assertEqual(caps.layout('22222222',8)[1],2)
        with self.assertRaises(ValueError):
            ClientCapabilities(minimum=4).layout('11',2)

    async def test_fallback_persistence_is_bounded_private_and_expires(self):
        with tempfile.TemporaryDirectory() as directory:
            now=[1000.]
            path=Path(directory)/'compatibility.json'
            registry=CompatibilityRegistry(path,clock=lambda:now[0])
            await registry.remember('127.0.0.1',['wallet.worker'],'firmware/1','extranonce')
            content=path.read_text()
            self.assertNotIn('wallet.worker',content)
            self.assertNotIn('127.0.0.1',content)
            restored=CompatibilityRegistry(path,clock=lambda:now[0]);restored.load()
            self.assertTrue(restored.requires_reconnect('127.0.0.1',['wallet.worker'],'firmware/1'))
            self.assertFalse(restored.requires_reconnect('127.0.0.1',['other.worker'],'firmware/1'))
            now[0]+=86401
            self.assertFalse(restored.requires_reconnect('127.0.0.1',['wallet.worker'],'firmware/1'))
            registry.path=None
            for n in range(2100):
                await registry.remember('127.0.0.1',[str(n)],'v','difficulty')
            self.assertLessEqual(len(registry.profiles),2048)

    async def test_delayed_checkpoint_cannot_replace_newer_profiles(self):
        with tempfile.TemporaryDirectory() as directory:
            registry=CompatibilityRegistry(Path(directory)/'profiles.json')
            registry._write('{"new":true}',2)
            registry._write('{"old":true}',1)
            self.assertEqual(json.loads(registry.path.read_text()),{'new':True})
