"""Bounded asynchronous pilot logs with producer-side secret redaction."""
import copy
from collections import deque
from datetime import datetime, timezone
import json
import logging
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler
from pathlib import Path
import queue
import re
import threading

_SECRET_VALUES = deque(maxlen=512)
_SECRET_LOCK = threading.Lock()


def remember_secret(value):
    if isinstance(value, str) and value and value != 'x':
        with _SECRET_LOCK:
            if value not in _SECRET_VALUES:
                _SECRET_VALUES.append(value)


def redact(text, extra_secrets=()):
    with _SECRET_LOCK:
        secrets = tuple(_SECRET_VALUES)
    variants = set()
    for value in secrets + tuple(v for v in extra_secrets if v and v != 'x'):
        variants.add(value)
        variants.add(repr(value)[1:-1])
        escaped = value
        for _ in range(3):
            escaped = json.dumps(escaped, ensure_ascii=True)[1:-1]
            variants.add(escaped)
    for value in sorted(variants, key=len, reverse=True):
        text = text.replace(value, '[REDACTED]')
    return re.sub(r'(?i)(Bearer\s+)[^\s"\\]+', r'\1[REDACTED]', text)


class SafeQueueHandler(QueueHandler):
    def __init__(self, messages):
        super().__init__(messages)
        self.dropped = 0

    def prepare(self, record):
        result = copy.copy(record)
        message = record.getMessage()
        if record.exc_info:
            message += '\n' + logging.Formatter().formatException(record.exc_info)
        result.msg, result.args = redact(message), ()
        result.exc_info = result.exc_text = result.stack_info = None
        return result

    def enqueue(self, record):
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            self.dropped += 1


class JsonFormatter(logging.Formatter):
    def format(self, record):
        message = record.getMessage()
        try:
            body = json.loads(message)
            if not isinstance(body, dict):
                body = {'message': message}
        except ValueError:
            body = {'message': message}
        body.update(timestamp=datetime.fromtimestamp(record.created, timezone.utc).isoformat(), level=record.levelname)
        return json.dumps(body, ensure_ascii=True, allow_nan=False)


class PilotLogging:
    def __init__(self, directory, level='INFO', max_bytes=50 * 1024 * 1024, backups=3, queue_size=2048):
        self.logger = logging.getLogger('mining_proxy')
        self.previous = (list(self.logger.handlers), self.logger.level, self.logger.propagate)
        Path(directory).mkdir(parents=True, exist_ok=True)
        self.file = RotatingFileHandler(Path(directory)/'proxy.log', maxBytes=max_bytes, backupCount=backups, encoding='utf-8')
        self.file.setFormatter(JsonFormatter())
        self.handler = SafeQueueHandler(queue.Queue(maxsize=queue_size))
        self.listener = QueueListener(self.handler.queue, self.file, respect_handler_level=True)
        self.logger.handlers = [self.handler]
        self.logger.setLevel(level)
        self.logger.propagate = False
        self.listener.start()

    def close(self):
        # Workers have already stopped; bounded waiting is only for the log drain.
        self.logger.handlers = self.previous[0]
        self.logger.setLevel(self.previous[1])
        self.logger.propagate = self.previous[2]
        try:
            self.handler.queue.put(self.listener._sentinel, timeout=5)
        except queue.Full:
            return False
        self.listener._thread.join(timeout=5)
        if self.listener._thread.is_alive():
            return False
        self.file.close()
        return True


def event(level, name, **fields):
    logging.getLogger('mining_proxy').log(level, json.dumps(dict(event=name, **fields), default=str, allow_nan=False))


def stratum_trace(direction, session_id, target, packet, passwords=()):
    logger = logging.getLogger('mining_proxy')
    if not logger.isEnabledFor(logging.DEBUG) or packet.get('method') not in ('mining.notify', 'mining.submit'):
        return
    safe = {key: packet[key] for key in ('id', 'method', 'params') if key in packet}
    if safe['method'] == 'mining.submit' and isinstance(safe.get('params'), list) and safe['params']:
        safe['params'] = ['[WORKER]'] + safe['params'][1:]
    payload = redact(json.dumps(safe, default=str, ensure_ascii=True), passwords)
    truncated = len(payload) > 16384
    event(logging.DEBUG, 'stratum_packet', direction=direction, session_id=session_id, target=target,
          packet=payload[:16384], truncated=truncated)
