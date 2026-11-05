import asyncio
import signal
import unittest
from shutdown_signals import ShutdownSignals


class ShutdownSignalTests(unittest.IsolatedAsyncioTestCase):
    async def test_sigterm_requests_cooperative_stop_and_restores_handler(self):
        previous=signal.getsignal(signal.SIGTERM)
        signals=ShutdownSignals()
        try:
            signals.install()
            signal.raise_signal(signal.SIGTERM)
            await asyncio.wait_for(signals.stop.wait(),2)
            self.assertTrue(signals.stop.is_set())
        finally:
            signals.close()
        self.assertEqual(signal.getsignal(signal.SIGTERM),previous)
