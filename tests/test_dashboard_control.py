import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
from dashboard_control import DashboardControl
from dashboard_server import DashboardStream, dashboard_request
from stratum_session import Relay


class ControlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)/'control.json'
        self.relay = Relay('127.0.0.1', 4444)
        self.stream = DashboardStream(self.relay.live_snapshot)
        self.control = DashboardControl(self.relay, self.stream, 'A'*32, self.path)
        self.server = await asyncio.start_server(lambda r,w: dashboard_request(r,w,self.relay.metrics,stream=self.stream,control=self.control), '127.0.0.1',0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        await self.stream.close()
        await self.control.observer_stream.close()
        self.server.close()
        await self.server.wait_closed()
        self.tmp.cleanup()

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
        DashboardControl(restored,self.stream,'A'*32,self.path)
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
