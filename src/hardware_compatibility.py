"""Capability negotiation and conservative, bounded client fallback profiles.

No vendor fingerprint is a compatibility certification. Pool extranonce bytes
are conserved exactly; version rolling must use one identical negotiated mask.
"""
import asyncio
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time
import threading
from collections import OrderedDict


def mask_value(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-fA-F]{8}', value):
        raise ValueError('invalid version mask')
    return int(value, 16)


def validate_config(params):
    if (not isinstance(params, list) or len(params) != 2 or not isinstance(params[0], list) or
            not all(isinstance(x, str) and x for x in params[0]) or not isinstance(params[1], dict)):
        raise ValueError('invalid configure parameters')
    if len(params[0]) > 32 or len(params[1]) > 128:
        raise ValueError('configure state limit exceeded')
    if 'version-rolling' in params[0]:
        mask_value(params[1].get('version-rolling.mask', 'ffffffff'))
        count = params[1].get('version-rolling.min-bit-count', 0)
        if type(count) is not int or not 0 <= count <= 32:
            raise ValueError('invalid minimum bit count')


def negotiated_mask(params, response):
    validate_config(params)
    result = response.get('result')
    if response.get('error') is not None or not isinstance(result, dict) or result.get('version-rolling') is not True:
        return None
    if 'version-rolling' not in params[0]:
        raise ValueError('unrequested version rolling')
    value = mask_value(result.get('version-rolling.mask'))
    if value & ~mask_value(params[1].get('version-rolling.mask', 'ffffffff')):
        raise ValueError('mask exceeds miner request')
    return f'{value:08x}'


def upstream_config(params, mask):
    if mask is None or 'version-rolling' not in params[0]:
        return params
    return [list(params[0]), dict(params[1], **{'version-rolling.mask': mask})]


def merge_config(previous, params, max_bytes=4096):
    if previous is None:
        merged = [list(params[0]), dict(params[1])]
    else:
        merged = [list(dict.fromkeys(previous[0] + params[0])), dict(previous[1], **params[1])]
    validate_config(merged)
    if len(json.dumps(merged, default=str, ensure_ascii=False, separators=(',',':')).encode()) > max_bytes:
        raise ValueError('merged configure state limit exceeded')
    return merged


class ClientCapabilities:
    def __init__(self, minimum=0, maximum=4):
        if type(minimum) is not int or type(maximum) is not int or not 0 <= minimum <= maximum <= 32:
            raise ValueError('invalid ASIC extranonce2 bounds')
        self.minimum, self.maximum = minimum, maximum
        self.preferred = None
        self.agent = ''

    def layout(self, extranonce1, upstream_size):
        if upstream_size < self.minimum:
            raise ValueError('upstream extranonce2 is below the configured ASIC minimum')
        size = min(upstream_size, self.preferred if self.preferred is not None else self.maximum)
        prefix = '00' * (upstream_size - size)
        return extranonce1 + prefix, size, prefix

    def subscription(self, result):
        extra, size, _ = self.layout(result[1], result[2])
        self.preferred = size
        return [result[0], extra, size, *result[3:]]


class CompatibilityRegistry:
    """Local hashed identities; one suspicious peer-close enables safe reconnect.

    Correlation is deliberately reported as a suspicion, not proven ASIC failure.
    Profiles expire in 24 hours, are bounded, and never contain pool passwords.
    """
    LIMIT = 2048
    TTL = 86400

    def __init__(self, path=None, clock=None):
        self.path = Path(path) if path else None
        self.clock = clock or time.time
        self.profiles = OrderedDict()
        self.lock = asyncio.Lock()
        self.write_lock = threading.Lock()
        self.generation = 0
        self.written_generation = 0
        self.next_prune = 0

    @staticmethod
    def identity(ip, worker, agent):
        return hashlib.sha256(json.dumps([ip, worker, agent[:128]], ensure_ascii=True).encode()).hexdigest()

    def prune(self, force=False):
        now = self.clock()
        if not force and now < self.next_prune and len(self.profiles) <= self.LIMIT:
            return
        self.next_prune = now + 60
        for key, value in list(self.profiles.items()):
            if now - value['seen'] >= self.TTL or value['seen'] > now + 300:
                self.profiles.pop(key, None)
        while len(self.profiles) > self.LIMIT:
            self.profiles.popitem(last=False)

    def load(self):
        if not self.path or not self.path.exists():
            return
        if self.path.stat().st_size > 1024 * 1024:
            raise ValueError('compatibility state too large')
        data = json.loads(self.path.read_text(encoding='utf-8'))
        if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('profiles'), dict):
            raise ValueError('invalid compatibility state')
        for key, value in data['profiles'].items():
            if (not re.fullmatch(r'[0-9a-f]{64}', key) or not isinstance(value, dict) or
                    type(value.get('seen')) not in (int,float) or not math.isfinite(value['seen']) or
                    value.get('reason') not in ('extranonce','difficulty')):
                raise ValueError('invalid compatibility profile')
            self.profiles[key] = {'seen': value['seen'], 'reason': value['reason']}
        self.prune(force=True)

    def requires_reconnect(self, ip, workers, agent):
        self.prune()
        now = self.clock()
        return any((entry := self.profiles.get(self.identity(ip, worker, agent))) is not None and
                   0 <= now - entry['seen'] < self.TTL for worker in workers)

    async def remember(self, ip, workers, agent, reason):
        async with self.lock:
            for worker in workers:
                self.profiles[self.identity(ip, worker, agent)] = {'seen': self.clock(), 'reason': reason}
            self.prune(force=True)
            if self.path:
                payload = json.dumps({'version':1,'profiles':self.profiles}, separators=(',',':'))
                self.generation += 1
                await asyncio.to_thread(self._write, payload, self.generation)

    def _write(self, payload, generation):
        with self.write_lock:
            if generation <= self.written_generation:
                return
            self._replace(payload)
            self.written_generation = generation

    def _replace(self, payload):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=self.path.parent, prefix='.compatibility-')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as file:
                file.write(payload)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
