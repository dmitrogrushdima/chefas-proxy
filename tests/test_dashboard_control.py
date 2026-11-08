import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, AsyncMock
from dashboard_control import DashboardControl
from dashboard_server import DashboardStream, dashboard_request
from stratum_session import Relay
from secret_store import SecretStore
from cryptography.fernet import Fernet


class ControlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)/'control.json'
        self.relay = Relay('127.0.0.1', 4444)
        self.stream = DashboardStream(self.relay.live_snapshot)
        self.secret_store = SecretStore(key=Fernet.generate_key(), environment={})
        self.control = DashboardControl(self.relay, self.stream, 'A'*32, self.path, secret_store=self.secret_store)
        self.server = await asyncio.start_server(lambda r,w: dashboard_request(r,w,self.relay.metrics,stream=self.stream,control=self.control), '127.0.0.1',0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        await self.stream.close()
        await self.control.observer_stream.close()
        self.server.close()
        await self.server.wait_closed()
        self.tmp.cleanup()

    async def test_tp_mode_requires_admin_and_persists(self):
        headers, _ = await self.request('/api/mode', data={'tp_mode': True})
        self.assertIn(b'403', headers)
        self.assertFalse(self.relay.tp_mode)
        headers, body = await self.request('/api/mode', 'A'*32, {'tp_mode': True})
        self.assertIn(b'200', headers)
        self.assertEqual(body['scope'], 'new_sessions')
        self.assertTrue(self.relay.tp_mode)
        self.assertTrue(json.loads(self.path.read_text())['tp_mode'])
        headers, _ = await self.request('/api/mode', 'A'*32, {'tp_mode': 'false'})
        self.assertIn(b'400', headers)
        self.assertTrue(self.relay.tp_mode)

    async def test_operator_release_cannot_override_fee_through_dashboard_or_state(self):
        self.relay.fee_locked = self.relay.active_router = True
        self.relay.fee = ('stratum+ssl://fee.example', 551)
        self.relay.fee_worker = 'release.worker'
        self.control._probe = AsyncMock()
        for path, payload in (('/api/mode', {'tp_mode': True}),
                              ('/api/config', {'devfee_pool': None})):
            headers, _ = await self.request(path, 'A'*32, payload)
            self.assertIn(b'400', headers)
        self.assertIsNone(self.control.public_config()['devfee_pool'])
        headers, _ = await self.request('/api/config', 'A'*32,
                                        {'user_pool': {'host': 'pool.example', 'port': 3333},
                                         'reconnect': False})
        self.assertIn(b'200', headers)
        self.assertNotIn('release.worker', self.path.read_text())
        self.assertEqual(self.relay.fee_worker, 'release.worker')
        restored = Relay('127.0.0.1', 4444)
        restored.fee_locked = True
        restored.fee = self.relay.fee
        restored.fee_worker = self.relay.fee_worker
        DashboardControl(restored, self.stream, state_path=self.path, secret_store=self.secret_store)
        self.assertEqual(restored.fee_worker, 'release.worker')

    async def request(self, path, token='', data=None, method=None):
        reader,writer=await asyncio.open_connection('127.0.0.1',self.port)
        try:
            payload=json.dumps(data).encode() if data is not None else b''
            verb=method or ('POST' if data is not None else 'GET')
            writer.write(f'{verb} {path} HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nAuthorization: Bearer {token}\r\nContent-Length: {len(payload)}\r\n\r\n'.encode()+payload)
            await writer.drain()
            response=await asyncio.wait_for(reader.read(),3)
            headers,body=response.split(b'\r\n\r\n',1)
            return headers,json.loads(body)
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_observer_cannot_mutate_or_read_private_endpoints(self):
        headers,link=await self.request('/api/observer-links','A'*32,{})
        self.assertIn(b'200 OK',headers)
        token=link['token']
        self.assertNotIn(token,self.path.read_text())
        for path in ['/api/blacklist','/api/workers/kick','/api/config']:
            headers,_=await self.request(path,token,{'blacklist':['127.0.0.1/32']})
            self.assertIn(b'403 Forbidden',headers)
        headers,_=await self.request('/api/config',token,method='DELETE')
        self.assertIn(b'403 Forbidden',headers)
        self.assertFalse(self.relay.blacklist)
        self.relay.events.append({'secret_log':True})
        headers,state=await self.request('/api/snapshot',token)
        self.assertIn(b'200 OK',headers)
        self.assertEqual(state['events'],[])
        self.assertEqual(state['status'],{})
        headers,_=await self.request('/api/status',token)
        self.assertIn(b'403 Forbidden',headers)
        headers,_=await self.request('/api/snapshot','bad')
        self.assertIn(b'403 Forbidden',headers)

    async def test_blacklist_persists_and_filters_before_session_creation(self):
        headers,_=await self.request('/api/blacklist','A'*32,{'blacklist':['127.0.0.1/32']})
        self.assertIn(b'200 OK',headers)
        restored=Relay('127.0.0.1',4444)
        DashboardControl(restored,self.stream,'A'*32,self.path,secret_store=self.secret_store)
        self.assertFalse(restored.peer_allowed(('127.0.0.1',123)))
        self.assertFalse(restored.peer_allowed(('::ffff:127.0.0.1',123)))
        miner_server=await asyncio.start_server(restored.handle,'127.0.0.1',0)
        try:
            reader,writer=await asyncio.open_connection('127.0.0.1',miner_server.sockets[0].getsockname()[1])
            self.assertEqual(await asyncio.wait_for(reader.read(),2),b'')
            self.assertEqual(restored.next_session_id,0)
            self.assertFalse(restored.sessions)
            writer.close()
            await writer.wait_closed()
        finally:
            miner_server.close()
            await miner_server.wait_closed()

    async def test_kick_requires_admin_and_targets_only_selected_sessions(self):
        first,second=Mock(),Mock()
        self.relay.sessions={1:{'id':1,'worker':'one','writer':first},2:{'id':2,'worker':'two','writer':second}}
        headers,_=await self.request('/api/workers/kick','bad',{'session_id':1})
        self.assertIn(b'403 Forbidden',headers)
        first.close.assert_not_called()
        headers,data=await self.request('/api/workers/kick','A'*32,{'session_id':1})
        self.assertIn(b'200 OK',headers)
        self.assertEqual(data['disconnected_sessions'],[1])
        first.close.assert_called_once()
        second.close.assert_not_called()

    async def test_config_persists_credentials_but_never_returns_passwords(self):
        self.relay.active_router = True
        self.control._probe = AsyncMock()
        headers, data = await self.request('/api/config', 'A'*32, {
            'user_pool': {'host':'127.0.0.1','port':5555,'worker':'wallet','password':'user-secret'},
            'devfee_pool': {'host':'127.0.0.1','port':6666,'worker':'fee','password':'fee-secret'}})
        self.assertIn(b'200 OK', headers)
        self.assertNotIn('user-secret', json.dumps(data))
        self.assertNotIn('fee-secret', json.dumps(data))
        self.assertNotIn('user-secret', self.path.read_text())
        self.assertNotIn('fee-secret', self.path.read_text())
        self.assertNotIn('A'*32, self.path.read_text())
        restored = Relay('127.0.0.1',4444)
        DashboardControl(restored, self.stream, 'A'*32, self.path,secret_store=self.secret_store)
        self.assertEqual((restored.host,restored.port,restored.user_worker,restored.user_password),
                         ('127.0.0.1',5555,'wallet','user-secret'))
        self.assertEqual((restored.fee,restored.fee_worker,restored.fee_password),
                         (('127.0.0.1',6666),'fee','fee-secret'))
        headers, _ = await self.request('/api/config','A'*32,{'generation':0,'reconnect':False})
        self.assertIn(b'400 Bad Request',headers)
        self.assertEqual(self.relay.route_generation,1)

    async def test_required_fee_cannot_be_disabled_by_config(self):
        self.relay.active_router = True
        self.relay.require_devfee = True
        self.relay.fee = ('127.0.0.1',5555)
        self.relay.fee_worker, self.relay.fee_password = 'fee','x'
        headers,_ = await self.request('/api/config','A'*32,{'devfee_pool':None})
        self.assertIn(b'400 Bad Request',headers)
        self.assertEqual(self.relay.fee,('127.0.0.1',5555))
        self.assertFalse(self.path.exists())

    async def test_concurrent_blacklist_additions_do_not_lose_updates(self):
        responses = await asyncio.gather(
            self.request('/api/blacklist','A'*32,{'add':'192.0.2.1'}),
            self.request('/api/blacklist','A'*32,{'add':'192.0.2.2'}))
        self.assertTrue(all(b'200 OK' in headers for headers,_ in responses))
        self.assertEqual({str(item) for item in self.relay.blacklist},{'192.0.2.1/32','192.0.2.2/32'})
