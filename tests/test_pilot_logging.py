import contextlib
import io
import json
import logging
from pathlib import Path
import queue
import tempfile
import unittest
from pilot_logging import PilotLogging, SafeQueueHandler, remember_secret, stratum_trace


class PilotLoggingTests(unittest.TestCase):
    def test_rotated_logs_redact_arguments_exceptions_and_packet_secrets(self):
        secret='test-secret-quote"-line\n-中文'
        remember_secret(secret)
        with tempfile.TemporaryDirectory() as directory:
            pilot=PilotLogging(directory,'DEBUG',max_bytes=1800,backups=3)
            logger=logging.getLogger('mining_proxy')
            try:
                for _ in range(30): logger.info('sensitive %s',secret)
                stratum_trace('rx',1,'user',{'method':'mining.authorize','params':['worker',secret]})
                stratum_trace('rx',1,'user',{'method':'mining.submit','params':['private-wallet',secret,'00000000','65000000','00000000']},(secret,))
                try: raise ValueError(secret)
                except ValueError: logger.exception('upstream failure')
            finally:
                self.assertTrue(pilot.close())
            files=list(Path(directory).glob('proxy.log*'))
            self.assertLessEqual(len(files),4)
            self.assertGreater(len(files),1)
            contents='\n'.join(path.read_text(encoding='utf-8') for path in files)
            self.assertNotIn('private-wallet',contents)
            self.assertNotIn('mining.authorize',contents)
            self.assertIn('[REDACTED]',contents)
            for line in contents.splitlines():
                if line:
                    data=json.loads(line)
                    self.assertNotIn(secret,data.get('message',''))
                    self.assertIn('timestamp',data)

    def test_full_queue_drops_without_blocking_or_printing_record(self):
        handler=SafeQueueHandler(queue.Queue(maxsize=1))
        record=logging.LogRecord('mining_proxy',logging.INFO,'',0,'Bearer private-observer-token',(),None)
        errors=io.StringIO()
        with contextlib.redirect_stderr(errors):
            handler.handle(record)
            handler.handle(record)
        self.assertEqual(handler.dropped,1)
        self.assertEqual(errors.getvalue(),'')
        self.assertNotIn('private-observer-token',handler.queue.get_nowait().getMessage())
