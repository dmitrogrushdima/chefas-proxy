"""Authenticated local management with atomic state commits and bounded actions."""
import asyncio
import hashlib
import hmac
import ipaddress
import json
import math
import os
from pathlib import Path
import secrets
import tempfile
import time
from collections import deque
from upstream_manager import PoolRoute, current_pool_routes, apply_pool_routes, open_upstream
from secret_store import SecretStore, SecretConfigurationError
from pilot_logging import remember_secret, event
import logging


class DashboardControl:
    def __init__(self, relay, stream, admin_token=None, state_path='state/control.json', secret_store=None):
        from dashboard_server import DashboardStream
        if admin_token is not None and len(admin_token) < 32:
            raise ValueError('KONTUR_ADMIN_TOKEN must contain at least 32 characters')
        self.relay, self.stream, self.admin_token = relay, stream, admin_token
        self.path = Path(state_path)
        self.secrets = secret_store or SecretStore()
        remember_secret(admin_token)
        self.tokens, self.route_data = {}, None
        self.mutations = deque()
        self.lock = asyncio.Lock()
        if self.path.exists():
            if self.path.stat().st_size > 2 * 1024 * 1024:
                raise ValueError('control state size limit exceeded')
            data = json.loads(self.path.read_text(encoding='utf-8'))
            self.relay.blacklist = tuple(ipaddress.ip_network(item, strict=True) for item in data.get('blacklist', []))
            self.tokens = data.get('observers', {})
            if len(self.relay.blacklist) > 256 or not isinstance(self.tokens, dict) or len(self.tokens) > 64:
                raise ValueError('control state limits exceeded')
            if any(len(key) != 64 or any(c not in '0123456789abcdef' for c in key) or
                   type(expiry) not in (int, float) or not math.isfinite(expiry) for key, expiry in self.tokens.items()):
                raise ValueError('invalid observer state')
            stored_routes = data.get('routes')
            self.route_data = self.secrets.open_routes(stored_routes)
            if self.route_data is not None:
                user = PoolRoute.parse(self.route_data['user_pool'])
                fee = PoolRoute.parse(self.route_data['devfee_pool']) if self.route_data['devfee_pool'] is not None else None
                if getattr(relay, 'require_devfee', False) and fee is None:
                    raise ValueError('required fee route cannot be disabled')
                apply_pool_routes(relay, user, fee)
                relay.route_generation = data.get('route_generation', 1)
                if type(relay.route_generation) is not int or relay.route_generation < 0:
                    raise ValueError('invalid route generation')
            legacy = stored_routes and any(isinstance(route, dict) and 'password' in route for route in stored_routes.values())
            if legacy or 'admin_token' in data or 'KONTUR_ADMIN_TOKEN' in data:
                self.save(self.relay.blacklist, self.tokens)
                event(logging.WARNING, 'control_secrets_migrated')
        self.observer_stream = DashboardStream(self.observer_snapshot)

    def observer_snapshot(self):
        snapshot = dict(self.stream.snapshot())
        snapshot['events'], snapshot['sessions'], snapshot['status'] = [], [], {}
        snapshot['access'] = 'observer'
        return snapshot

    def save(self, blacklist, tokens, routes=None, generation=None):
        sealed_routes = self.secrets.seal_routes(self.route_data if routes is None else routes)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(dir=self.path.parent, prefix='.control-')
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as file:
                json.dump({'blacklist': [str(item) for item in blacklist], 'observers': tokens,
                           'routes': sealed_routes,
                           'route_generation': getattr(self.relay, 'route_generation', 0) if generation is None else generation}, file)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    async def respond(self, writer, status, body):
        encoded = json.dumps(body, allow_nan=False).encode()
        writer.write(b'HTTP/1.1 ' + status + b'\r\nContent-Type: application/json\r\nCache-Control: no-store\r\nReferrer-Policy: no-referrer\r\nContent-Length: ' + str(len(encoded)).encode() + b'\r\nConnection: close\r\n\r\n' + encoded)
        await asyncio.wait_for(writer.drain(), 3)

    async def _reconnect(self, session):
        router = session.get('router')
        try:
            if router:
                await asyncio.wait_for(router.send({'id': None, 'method': 'client.reconnect', 'params': []}), 1)
        except (OSError, asyncio.TimeoutError):
            pass
        finally:
            session['writer'].close()

    async def _probe(self, route):
        endpoint = (route.host, route.port)
        if await self.relay.is_self_route(endpoint):
            raise ValueError('route points to listener')
        _, writer = await open_upstream(endpoint, self.relay.tls_context, self.relay.timeout,
                                        getattr(self.relay, 'url_tls_context', None))
        writer.close()
        try:
            await asyncio.wait_for(writer.wait_closed(), 1)
        except (OSError, asyncio.TimeoutError):
            pass

    def public_config(self):
        user, fee = current_pool_routes(self.relay)
        return {'generation': getattr(self.relay, 'route_generation', 0), 'user_pool': user.export(),
                'devfee_pool': fee.export() if fee else None, 'require_devfee': getattr(self.relay, 'require_devfee', False)}

    async def mutate(self, path, data):
        if path == '/api/blacklist':
            values = data.get('blacklist')
            if 'add' in data or 'remove' in data:
                networks = set(self.relay.blacklist)
                if 'add' in data:
                    networks.add(ipaddress.ip_network(data['add'], strict=True))
                if 'remove' in data:
                    networks.discard(ipaddress.ip_network(data['remove'], strict=True))
                values = [str(item) for item in networks]
            if not isinstance(values, list) or len(values) > 256 or not all(isinstance(v, str) for v in values):
                raise ValueError('invalid blacklist')
            networks = tuple(ipaddress.ip_network(value, strict=True) for value in sorted(set(values)))
            await asyncio.to_thread(self.save, networks, self.tokens)
            self.relay.blacklist = networks
            return {'blacklist': [str(item) for item in networks]}
        if path == '/api/workers/kick':
            if type(data.get('session_id')) is int:
                ids = {data['session_id']}
            elif isinstance(data.get('worker_id'), str):
                ids = set().union(*(row['sessions'] for row in self.relay.workers.entries.values() if row['id'] == data['worker_id']))
            elif isinstance(data.get('worker'), str):
                ids = {item['id'] for item in self.relay.sessions.values() if item['worker'] == data['worker'] or
                       data['worker'] in getattr(item.get('observer'), 'protocol', {}).get('authorized_workers', [])}
            else:
                raise ValueError('session_id, worker_id or worker required')
            selected = [item for item in self.relay.sessions.values() if item['id'] in ids]
            for item in selected:
                item['writer'].close()
            return {'disconnected_sessions': [item['id'] for item in selected]}
        if path == '/api/observer-links':
            tokens = {key: expiry for key, expiry in self.tokens.items() if expiry > time.time()}
            if len(tokens) >= 64:
                raise ValueError('observer limit reached')
            secret = secrets.token_urlsafe(32)
            remember_secret(secret)
            expires = time.time() + 3600
            tokens[hashlib.sha256(secret.encode()).hexdigest()] = expires
            await asyncio.to_thread(self.save, self.relay.blacklist, tokens)
            self.tokens = tokens
            return {'token': secret, 'expires_unix': expires, 'fragment': '#observer=' + secret}
        if path == '/api/config':
            if not self.relay.active_router or set(data) - {'user_pool', 'devfee_pool', 'reconnect', 'generation'}:
                raise ValueError('active routing required')
            if 'generation' in data and (type(data['generation']) is not int or
                                         data['generation'] != getattr(self.relay, 'route_generation', 0)):
                raise ValueError('stale configuration generation')
            reconnect = data.get('reconnect', True)
            if type(reconnect) is not bool:
                raise ValueError('invalid reconnect policy')
            user, fee = current_pool_routes(self.relay)
            if 'user_pool' in data:
                user = PoolRoute.parse(data['user_pool'], user)
            if 'devfee_pool' in data:
                fee = PoolRoute.parse(data['devfee_pool'], fee) if data['devfee_pool'] is not None else None
            if fee is not None and not fee.worker:
                raise ValueError('fee worker required')
            if getattr(self.relay, 'require_devfee', False) and fee is None:
                raise ValueError('required fee cannot be disabled')
            await asyncio.wait_for(self._probe(user), 4)
            if fee:
                await asyncio.wait_for(self._probe(fee), 4)
            generation = getattr(self.relay, 'route_generation', 0) + 1
            stored = {'user_pool': user.export(True), 'devfee_pool': fee.export(True) if fee else None}
            await asyncio.to_thread(self.save, self.relay.blacklist, self.tokens, stored, generation)
            self.route_data = stored
            apply_pool_routes(self.relay, user, fee)
            event(logging.INFO, 'routes_changed', generation=generation, reconnect=reconnect)
            selected = list(self.relay.sessions.values()) if reconnect else []
            await asyncio.gather(*(self._reconnect(item) for item in selected))
            return dict(self.public_config(), reconnect_requested=[item['id'] for item in selected])
        raise KeyError(path)

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
        token = auth[7:] if auth.startswith('Bearer ') else parse_qs(urlsplit(target).query).get('observer', [''])[0]
        admin = bool(self.admin_token and auth.startswith('Bearer ') and hmac.compare_digest(token.encode(), self.admin_token.encode()))
        digest = hashlib.sha256(token.encode()).hexdigest()
        observer = bool(token and self.tokens.get(digest, 0) > time.time())
        if path == '/api/access' and method == 'GET':
            await self.respond(writer, b'200 OK', {'role': 'admin' if admin else 'observer' if observer else
                                                 'unauthenticated' if self.admin_token or token else 'public',
                                                 'admin_configured': self.admin_token is not None})
            return True
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
            if path == '/api/config':
                await self.respond(writer, b'200 OK' if admin else b'403 Forbidden', self.public_config() if admin else {'error': 'administrator required'})
                return True
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
                raise ValueError('unsupported transfer encoding')
            size = int(headers.get(b'content-length', '0'))
            if not 0 < size <= 16384:
                raise ValueError('invalid body size')
            data = json.loads(await asyncio.wait_for(reader.readexactly(size), 3))
            if not isinstance(data, dict):
                raise ValueError('object required')
            async with self.lock:
                result = await self.mutate(path, data)
            await self.respond(writer, b'200 OK', result)
        except SecretConfigurationError:
            await self.respond(writer, b'503 Service Unavailable', {'error': 'Configure pool password ENV or KONTUR_SECRETS_KEY before saving secrets'})
        except (ValueError, TypeError, OverflowError):
            await self.respond(writer, b'400 Bad Request', {'error': 'invalid request or stale configuration'})
        except KeyError:
            await self.respond(writer, b'404 Not Found', {'error': 'unknown action'})
        except (OSError, asyncio.TimeoutError):
            await self.respond(writer, b'503 Service Unavailable', {'error': 'state or upstream unavailable'})
        return True
