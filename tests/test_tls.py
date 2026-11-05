import asyncio
import ssl
import unittest
from pathlib import Path
from unittest.mock import patch

from proxy import Relay


FIXTURES = Path(__file__).parent / "fixtures"


class TLSRelayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(FIXTURES / "lab-cert.pem", FIXTURES / "lab-key.pem")

        async def pool(reader, writer):
            try:
                data = await reader.readline()
                writer.write(data)
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        self.pool = await asyncio.start_server(pool, "127.0.0.1", 0, ssl=context)
        self.pool_port = self.pool.sockets[0].getsockname()[1]
        self.server = None
        self.relay = None

    async def asyncTearDown(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        if self.relay:
            await self.relay.shutdown()
        self.pool.close()
        await self.pool.wait_closed()

    async def run_relay(self, host, context, tls=True):
        self.relay = Relay(host, self.pool_port, tls=tls, tls_context=context, timeout=3)
        self.server = await asyncio.start_server(self.relay.handle, "127.0.0.1", 0)
        reader, writer = await asyncio.open_connection("127.0.0.1", self.server.sockets[0].getsockname()[1])
        writer.write(b"hello\n")
        await writer.drain()
        try:
            return await asyncio.wait_for(reader.read(), 6)
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_valid_certificate_and_hostname(self):
        context = ssl.create_default_context(cafile=str(FIXTURES / "lab-cert.pem"))
        self.assertEqual(await self.run_relay("localhost", context), b"hello\n")
        self.assertEqual(self.relay.metrics.upstream_failures, 0)

    async def test_ssl_url_enables_tls_without_global_flag(self):
        context = ssl.create_default_context(cafile=str(FIXTURES/'lab-cert.pem'))
        with patch('upstream_manager.default_tls_context',return_value=context):
            self.assertEqual(await self.run_relay('stratum+ssl://localhost',None,tls=False),b'hello\n')

    async def test_ssl_url_rejects_untrusted_certificate_without_plaintext_retry(self):
        self.assertEqual(await self.run_relay('stratum+ssl://localhost',None,tls=False),b'')
        self.assertEqual(self.relay.metrics.upstream_failures,1)

    async def test_untrusted_certificate_rejected(self):
        context = ssl.create_default_context()
        self.assertEqual(await self.run_relay("localhost", context), b"")
        self.assertEqual(self.relay.metrics.upstream_failures, 1)

    async def test_hostname_mismatch_rejected(self):
        context = ssl.create_default_context(cafile=str(FIXTURES / "lab-cert.pem"))
        self.assertEqual(await self.run_relay("127.0.0.1", context), b"")
        self.assertEqual(self.relay.metrics.upstream_failures, 1)

    async def test_insecure_context_refused(self):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        with self.assertRaises(ValueError):
            Relay("localhost", self.pool_port, tls=True, tls_context=context)
