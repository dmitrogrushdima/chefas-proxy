"""Atomic worker-history checkpoints; all writes serialized in one background task."""
import asyncio
import json
import logging
import os
from pathlib import Path
import tempfile
import threading
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from fractions import Fraction


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


class DevFeeJournal:
    """Durable daily totals. Persist exact confirmed work before replying to ASICs."""

    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.database = sqlite3.connect(path, check_same_thread=False)
        except sqlite3.Error as exc:
            raise ValueError('Cannot open DevFee journal') from exc
        try:
            self.database.execute('PRAGMA journal_mode=WAL')
            self.database.execute('PRAGMA synchronous=FULL')
            self.database.execute('''CREATE TABLE IF NOT EXISTS work_v1 (
                day TEXT NOT NULL, route TEXT NOT NULL, pool TEXT NOT NULL,
                accepted INTEGER NOT NULL, rejected INTEGER NOT NULL,
                unknown INTEGER NOT NULL, difficulty TEXT NOT NULL,
                PRIMARY KEY (day, route, pool)
            )''')
            self.database.execute('CREATE TABLE IF NOT EXISTS fee_eligible_v1 (id INTEGER PRIMARY KEY, difficulty TEXT NOT NULL)')
            self.database.execute('CREATE TABLE IF NOT EXISTS tenant_budget_v1 (tenant TEXT, route TEXT, accepted INTEGER, difficulty TEXT, eligible TEXT, PRIMARY KEY (tenant,route))')
            self.database.execute('CREATE TABLE IF NOT EXISTS fee_loss_v1 (tenant TEXT PRIMARY KEY, difficulty TEXT NOT NULL, shares INTEGER NOT NULL)')
            self.database.execute('CREATE TABLE IF NOT EXISTS fee_loss_scope_v1 (id INTEGER PRIMARY KEY CHECK (id=1), scope TEXT NOT NULL)')
            self.database.commit()
        except sqlite3.Error as exc:
            self.database.close()
            raise ValueError('Cannot initialize DevFee journal') from exc
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='devfee-journal')
        self.error = None
        self.closed = False

    def _commit(self, day, route, pool, outcome, difficulty, fee_eligible=False, tenant="main", compensated_loss=False):
        key = (day, route, pool)
        with self.database:
            self.database.execute('BEGIN IMMEDIATE')
            if compensated_loss:
                if route != 'user' or outcome != 'rejected' or Fraction(str(difficulty)) <= 0:
                    raise ValueError('invalid protected loss')
                row = self.database.execute('SELECT difficulty,shares FROM fee_loss_v1 WHERE tenant=?', (tenant,)).fetchone()
                loss, count = row or ('0', 0)
                self.database.execute('INSERT OR REPLACE INTO fee_loss_v1 VALUES (?,?,?)',
                                      (tenant, str(Fraction(loss) + Fraction(str(difficulty))), count + 1))
            row = self.database.execute(
                'SELECT accepted, rejected, unknown, difficulty FROM work_v1 WHERE day=? AND route=? AND pool=?',
                key).fetchone()
            accepted, rejected, unknown, work = row or (0, 0, 0, '0')
            if outcome == 'accepted':
                work = str(Fraction(work) + Fraction(str(difficulty)))
            if outcome == 'accepted':
                row = self.database.execute('SELECT accepted,difficulty,eligible FROM tenant_budget_v1 WHERE tenant=? AND route=?',
                                            (tenant, route)).fetchone()
                count, total, eligible_work = row or (0, '0', '0')
                work_delta = Fraction(str(difficulty))
                self.database.execute('INSERT OR REPLACE INTO tenant_budget_v1 VALUES (?,?,?,?,?)',
                                      (tenant, route, count + 1, str(Fraction(total) + work_delta),
                                       str(Fraction(eligible_work) + (work_delta if fee_eligible else 0))))
            if outcome == 'accepted' and fee_eligible:
                row = self.database.execute('SELECT difficulty FROM fee_eligible_v1 WHERE id=1').fetchone()
                eligible = Fraction(row[0] if row else 0) + Fraction(str(difficulty))
                self.database.execute('INSERT OR REPLACE INTO fee_eligible_v1 VALUES (1, ?)', (str(eligible),))
            self.database.execute('INSERT OR REPLACE INTO work_v1 VALUES (?, ?, ?, ?, ?, ?, ?)',
                                  (*key, accepted + (outcome == 'accepted'),
                                   rejected + (outcome == 'rejected'), unknown + (outcome == 'unknown'), work))

    def _record(self, day, route, pool, outcome, difficulty, fee_eligible=False, tenant="main", compensated_loss=False):
        try:
            self._commit(day, route, pool, outcome, difficulty, fee_eligible, tenant, compensated_loss)
        except (sqlite3.Error, OSError, ValueError):
            if self.error is None:
                logging.getLogger('mining_proxy').error('DevFee journal write failed')
            self.error = 'DevFee journal write failed'
            return False
        return True

    async def record(self, route, pool, outcome, difficulty, fee_eligible=False, tenant="main", *, compensated_loss=False):
        day = datetime.now(timezone.utc).date().isoformat()
        return await asyncio.shield(asyncio.get_running_loop().run_in_executor(
            self.executor, self._record, day, route, pool, outcome, difficulty, fee_eligible, tenant, compensated_loss))

    def restore_budget(self, ledger, tenant=None):
        if tenant is None:
            rows = self.database.execute("SELECT route,accepted,difficulty,'0' FROM work_v1")
        else:
            rows = self.database.execute(
                'SELECT route,accepted,difficulty,eligible FROM tenant_budget_v1 WHERE tenant=?', (tenant,))
        for route, accepted, difficulty, eligible in rows:
            ledger.eligible_user_work += Fraction(eligible)
            if route == "fee":
                ledger.fee_work += Fraction(difficulty)
                ledger.fee_shares += accepted
            else:
                ledger.user_work += Fraction(difficulty)
                ledger.user_shares += accepted
        row = self.database.execute('SELECT difficulty FROM fee_eligible_v1 WHERE id=1').fetchone()
        if tenant is None:
            ledger.eligible_user_work = Fraction(row[0]) if row else Fraction(0)
        # Already granted reductions remain honored when new protection is off.
        if tenant is None:
            rows = self.database.execute('SELECT difficulty,shares FROM fee_loss_v1')
        else:
            rows = self.database.execute('SELECT difficulty,shares FROM fee_loss_v1 WHERE tenant=?', (tenant,))
        for difficulty, shares in rows:
            ledger.protected_loss_work += Fraction(difficulty)
            ledger.protected_loss_shares += shares
        ledger._user_since_window = max(Fraction(0), ledger.outstanding * 99)

    def bind_loss_scope(self, scope):
        """Startup-only binding prevents transferring historical credits to another account."""
        with self.database:
            row = self.database.execute('SELECT scope FROM fee_loss_scope_v1 WHERE id=1').fetchone()
            if row is not None and row[0] != scope:
                raise ValueError('Protected fee journal belongs to another worker/pool; keep its original scope')
            if scope is not None:
                self.database.execute('INSERT OR IGNORE INTO fee_loss_scope_v1 VALUES (1,?)', (scope,))

    def _snapshot(self):
        totals = {}
        recent = []
        rows = self.database.execute('SELECT * FROM work_v1 ORDER BY day DESC')
        for day, route, pool, accepted, rejected, unknown, work in rows:
            entry = totals.setdefault((route, pool), [0, 0, 0, Fraction(0)])
            entry[0] += accepted
            entry[1] += rejected
            entry[2] += unknown
            entry[3] += Fraction(work)
            if len(recent) < 1000:
                recent.append(dict(day_utc=day, route=route, pool=pool, accepted=accepted,
                                   rejected=rejected, unknown=unknown, accepted_difficulty=work))
        return {'scope': 'routed share outcomes recorded locally; accepted work uses exact fractions',
                'totals': [dict(route=route, pool=pool, accepted=a, rejected=r, unknown=u,
                                accepted_difficulty=str(work))
                           for (route, pool), (a, r, u, work) in sorted(totals.items())],
                'recent_daily_rows': recent}

    async def snapshot(self):
        if self.closed:
            return {'error': 'DevFee journal is closing'}
        try:
            result = await asyncio.get_running_loop().run_in_executor(self.executor, self._snapshot)
        except (sqlite3.Error, OSError, ValueError):
            return {'error': 'DevFee journal read failed'}
        return dict(result, error=self.error)

    async def close(self):
        self.closed = True
        try:
            await asyncio.get_running_loop().run_in_executor(self.executor, self.database.close)
        finally:
            self.executor.shutdown(wait=False)
