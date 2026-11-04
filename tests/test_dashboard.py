import asyncio
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

    async def request(self, path, host):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        writer.write(f"GET {path} HTTP/1.1\r\nHost: {host}\r\n\r\n".encode())
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
        self.assertIn(b"text/css", await self.request("/app.css", host))
        self.assertIn(b"application/javascript", await self.request("/app.js", host))
        self.assertIn(b"404 Not Found", await self.request("/missing", host))
        self.assertIn(b"[]", await self.request("/api/sessions", host))
        self.assertIn(b"{}", await self.request("/api/status", host))
        self.assertIn(b"404 Not Found", await self.request("/../proxy.py", host))

    async def test_rejects_unexpected_host(self):
        self.assertIn(b"403 Forbidden", await self.request("/api/metrics", "attacker.example"))
