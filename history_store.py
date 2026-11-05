"""Atomic worker-history checkpoints; all writes serialized in one background task."""
import asyncio
import json
import logging
import os
from pathlib import Path
import tempfile
import threading
from datetime import datetime, timezone


class HistoryStore:
    def __init__(self, registry, path, interval=60):
        self.registry, self.path, self.interval = registry, Path(path), interval
        self.stopping = asyncio.Event()
        self.task = None
        self.write_lock = threading.Lock()
        self.generation = 0
        self.written_generation = 0
        registry.persistence['enabled'] = True

    def load(self):
        if self.path.exists():
            if self.path.stat().st_size > 128 * 1024 * 1024:
                raise ValueError('history exceeds size limit')
            with self.path.open(encoding='utf-8') as file:
                self.registry.restore_history(json.load(file))

    def _write(self, data, generation):
        # Cancellation does not stop an executor thread. A delayed older write
        # must not replace the final shutdown checkpoint.
        with self.write_lock:
            if generation <= self.written_generation:
                return
            self._replace(data)
            self.written_generation = generation

    def _replace(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=self.path.parent, prefix='.history-')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as file:
                json.dump(data, file, allow_nan=False, separators=(',', ':'))
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    async def checkpoint(self):
        try:
            data = self.registry.dump_history()
            self.generation += 1
            await asyncio.to_thread(self._write, data, self.generation)
            self.registry.persistence.update(last_saved_utc=datetime.now(timezone.utc).isoformat(), error=None)
        except (OSError, ValueError):
            self.registry.persistence['error'] = 'History checkpoint failed'
            logging.getLogger('mining_proxy').error('Worker history checkpoint failed')

    async def _run(self):
        while not self.stopping.is_set():
            try:
                await asyncio.wait_for(self.stopping.wait(), self.interval)
            except asyncio.TimeoutError:
                await self.checkpoint()

    def start(self):
        self.task = asyncio.create_task(self._run())

    async def close(self):
        self.stopping.set()
        if self.task:
            try:
                await self.task
            except asyncio.CancelledError:
                # asyncio.run on Python 3.10 cancels background tasks together
                # with the main coroutine during Ctrl+C.
                pass
        await self.checkpoint()
