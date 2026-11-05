"""Local authenticated administration. Observer secrets are stored as hashes."""
import asyncio
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import secrets
import tempfile
import time
from collections import deque


class DashboardControl:
    def __init__(self, relay, stream, admin_token=None, state_path='state/control.json'):
        from dashboard_server import DashboardStream
        if admin_token is not None and len(admin_token) < 32:
            raise ValueError('KONTUR_ADMIN_TOKEN must contain at least 32 characters')
        self.relay, self.stream, self.admin_token = relay, stream, admin_token
        self.path = Path(state_path)
        self.tokens = {}
        self.mutations = deque()
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding='utf-8'))
            self.relay.blacklist = tuple(ipaddress.ip_network(item, strict=True) for item in data.get('blacklist', []))
            self.tokens = data.get('observers', {})
        self.observer_stream = DashboardStream(self.observer_snapshot)

    def observer_snapshot(self):
        snapshot = dict(self.stream.snapshot())
        snapshot['events'], snapshot['sessions'], snapshot['status'] = [], [], {}
        snapshot['access'] = 'observer'
        return snapshot

    def save(self, blacklist, tokens):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(dir=self.path.parent, prefix='.control-')
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as file:
                json.dump({'blacklist': [str(item) for item in blacklist], 'observers': tokens}, file)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    async def respond(self, writer, status, body):
        encoded = json.dumps(body).encode()
        writer.write(b'HTTP/1.1 ' + status + b'\r\nContent-Type: application/json\r\nCache-Control: no-store\r\nReferrer-Policy: no-referrer\r\nContent-Length: ' + str(len(encoded)).encode() + b'\r\nConnection: close\r\n\r\n' + encoded)
        await asyncio.wait_for(writer.drain(), 3)

    async def handle(self, reader, writer, method, target, raw_headers):
        from urllib.parse import urlsplit, parse_qs
        path = urlsplit(target).path
        if not path.startswith('/api/'):
            return False
        headers = {}
        for line in raw_headers:
            if b':' in line:
                key, value = line.split(b':', 1)
                key = key.strip().lower()
                if key in headers:
                    await self.respond(writer, b'403 Forbidden', {'error': 'duplicate header'})
                    return True
                headers[key] = value.strip().decode('latin1')
        auth = headers.get(b'authorization', '')
        query_token = parse_qs(urlsplit(target).query).get('observer', [''])[0]
        token = auth[7:] if auth.startswith('Bearer ') else query_token
        admin = bool(self.admin_token and auth.startswith('Bearer ') and hmac.compare_digest(token.encode(), self.admin_token.encode()))
        digest = hashlib.sha256(token.encode()).hexdigest()
        observer = bool(token and self.tokens.get(digest, 0) > time.time())
        if observer and method == 'GET':
            if path == '/api/stream':
                await self.observer_stream.serve(reader, writer, lambda: self.tokens.get(digest, 0) > time.time())
            elif path in ('/api/snapshot', '/api/workers', '/api/metrics'):
                data = self.observer_snapshot()
                await self.respond(writer, b'200 OK', data['registry'] if path.endswith('workers') else data['metrics'] if path.endswith('metrics') else data)
            else:
                await self.respond(writer, b'403 Forbidden', {'error': 'observer scope'})
            return True
        if not admin and (method != 'GET' or self.admin_token or token):
            await self.respond(writer, b'403 Forbidden', {'error': 'administrator required'})
            return True
        if method == 'GET':
            if path == '/api/blacklist':
                await self.respond(writer, b'200 OK', {'blacklist': [str(item) for item in self.relay.blacklist]})
                return True
            return False
        if not admin or method != 'POST':
            await self.respond(writer, b'403 Forbidden', {'error': 'administrator required'})
            return True
        now = time.monotonic()
        while self.mutations and now - self.mutations[0] > 60:
            self.mutations.popleft()
        if len(self.mutations) >= 30:
            await self.respond(writer, b'429 Too Many Requests', {'error': 'rate limit'})
            return True
        self.mutations.append(now)
        try:
            if b'transfer-encoding' in headers:
                raise ValueError('transfer encoding is unsupported')
            size = int(headers.get(b'content-length', '0'))
            if not 0 < size <= 16384:
                raise ValueError('body size must be 1..16384')
            data = json.loads(await asyncio.wait_for(reader.readexactly(size), 3))
            if not isinstance(data, dict):
                raise ValueError('object required')
            if path == '/api/blacklist':
                values = data.get('blacklist')
                if not isinstance(values, list) or len(values) > 256 or not all(isinstance(v, str) for v in values):
                    raise ValueError('blacklist must contain up to 256 IP networks')
                networks = tuple(ipaddress.ip_network(value, strict=True) for value in values)
                self.save(networks, self.tokens)
                self.relay.blacklist = networks
                result = {'blacklist': [str(item) for item in networks]}
            elif path == '/api/workers/kick':
                selected = []
                if type(data.get('session_id')) is int:
                    selected = [item for item in self.relay.sessions.values() if item['id'] == data['session_id']]
                elif isinstance(data.get('worker'), str):
                    selected = [item for item in self.relay.sessions.values() if item['worker'] == data['worker']]
                else:
                    raise ValueError('session_id or worker required')
                for item in selected:
                    item['writer'].close()
                result = {'disconnected_sessions': [item['id'] for item in selected]}
            elif path == '/api/observer-links':
                tokens = {key: expiry for key, expiry in self.tokens.items() if expiry > time.time()}
                if len(tokens) >= 64:
                    raise ValueError('observer limit reached')
                secret = secrets.token_urlsafe(32)
                expires = time.time() + 3600
                tokens[hashlib.sha256(secret.encode()).hexdigest()] = expires
                self.save(self.relay.blacklist, tokens)
                self.tokens = tokens
                result = {'token': secret, 'expires_unix': expires, 'fragment': '#observer=' + secret}
            else:
                await self.respond(writer, b'404 Not Found', {'error': 'unknown action'})
                return True
            await self.respond(writer, b'200 OK', result)
        except (ValueError, TypeError, OverflowError):
            await self.respond(writer, b'400 Bad Request', {'error': 'invalid request'})
        except OSError:
            await self.respond(writer, b'503 Service Unavailable', {'error': 'state could not be saved'})
        return True
