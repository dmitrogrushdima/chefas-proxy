import asyncio
import json
import unittest

from observer import Metrics
from proxy import dashboard_request


class DashboardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = await asyncio.start_server(
            lambda r, w: dashboard_request(r, w, Metrics()), "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        self.server.close()
        await self.server.wait_closed()

    async def request(self, path, host, extra_headers=()):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        fields = ''.join(header + '\r\n' for header in extra_headers)
        writer.write(f"GET {path} HTTP/1.1\r\nHost: {host}\r\n{fields}\r\n".encode())
        await writer.drain()
        response = await reader.read()
        writer.close()
        await writer.wait_closed()
        return response

    async def test_metrics_and_dashboard(self):
        host = f"127.0.0.1:{self.port}"
        self.assertIn(b'"active": 0', await self.request("/api/metrics", host))
        html = await self.request("/", host)
        self.assertIn(b"200 OK", html)
        self.assertIn(b"Content-Security-Policy:", html)
        self.assertIn(b"/app.js", html)
        self.assertIn(b"/i18n.js", html)
        self.assertIn(b'<html lang="en">', html)
        self.assertIn(b"text/css", await self.request("/app.css", host))
        self.assertIn(b"application/javascript", await self.request("/app.js", host))
        self.assertIn(b"application/javascript", await self.request("/i18n.js", host))
        self.assertIn(b"404 Not Found", await self.request("/missing", host))
        self.assertIn(b"[]", await self.request("/api/sessions", host))
        self.assertIn(b"{}", await self.request("/api/status", host))
        self.assertIn(b"404 Not Found", await self.request("/../proxy.py", host))

    async def test_rejects_unexpected_host(self):
        self.assertIn(b"403 Forbidden", await self.request("/api/metrics", "attacker.example"))

    async def test_installation_link_can_open_shell_but_not_cross_site_api_or_frame(self):
        host = f'127.0.0.1:{self.port}'
        navigation = ('Sec-Fetch-Site: cross-site', 'Sec-Fetch-Mode: navigate',
                      'Sec-Fetch-Dest: document')
        for path in ('/', '/index.html'):
            self.assertIn(b'200 OK', await self.request(path, host, navigation))
        for path in ('/api/snapshot', '/api/access', '/api/audit-export', '/metrics'):
            self.assertIn(b'403 Forbidden', await self.request(path, host, navigation))
        self.assertIn(b'403 Forbidden', await self.request('/', host,
                      ('Sec-Fetch-Site: cross-site', 'Sec-Fetch-Mode: navigate', 'Sec-Fetch-Dest: iframe')))
        self.assertIn(b'403 Forbidden', await self.request('/', 'attacker.example', navigation))
        self.assertIn(b'403 Forbidden', await self.request('/', host,
                      (*navigation, 'Origin: https://attacker.example')))

    async def test_diagnostic_export_does_not_invent_fee_or_difficulty(self):
        response = await self.request("/api/audit-export", f"127.0.0.1:{self.port}")
        headers, body = response.split(b"\r\n\r\n", 1)
        self.assertIn(b"Content-Disposition: attachment", headers)
        data = json.loads(body)
        self.assertFalse(data["devfee_enabled"])
        self.assertIsNone(data["accepted_difficulty"])
        self.assertIsNone(data["fee_percentage"])
        self.assertIn("not a financial audit", data["scope"])

    async def test_diagnostic_export_excludes_protocol_details_and_extra_fields(self):
        sample = [{"id": 1, "worker": "alice.worker", "state": "connected",
                   "protocol_observation": {"extranonce1": "secret-marker"}, "password": "password-marker"}]
        server = await asyncio.start_server(
            lambda r, w: dashboard_request(r, w, Metrics(), lambda: sample), "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(f"GET /api/audit-export HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
            await writer.drain()
            response = await reader.read()
            writer.close()
            await writer.wait_closed()
            self.assertIn(b"alice.worker", response)
            self.assertNotIn(b"secret-marker", response)
            self.assertNotIn(b"password-marker", response)
        finally:
            server.close()
            await server.wait_closed()
