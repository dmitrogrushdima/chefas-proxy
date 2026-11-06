"""Best-effort, passive Stratum V1 observations; never controls forwarding."""
import json
import time
import math
import string
import hashlib
import ipaddress
from datetime import datetime, timezone
from collections import OrderedDict, deque
from fractions import Fraction


class WorkAudit:
    """Pool-confirmed accepted work, bucketed by monotonic second."""

    def __init__(self, ledger, clock=time.monotonic):
        self.ledger, self.clock = ledger, clock
        self.started = clock()
        self.buckets = deque()

    def record(self, target, difficulty):
        second = int(self.clock())
        if not self.buckets or self.buckets[-1][0] != second:
            self.buckets.append([second, Fraction(0), Fraction(0)])
        self.buckets[-1][1 if target == "user" else 2] += Fraction(str(difficulty))
        self._trim(second)

    def _trim(self, now):
        while self.buckets and self.buckets[0][0] <= now - 3600:
            self.buckets.popleft()

    def snapshot(self):
        now = self.clock()
        self._trim(int(now))
        result = self.ledger.snapshot()
        result["scope"] = "pool-confirmed difficulty since process start; resets on restart"
        result["hashrate_windows"] = {}
        elapsed = max(0.0, now - self.started)
        for label, window in (("5m", 300), ("15m", 900), ("1h", 3600)):
            measured = min(elapsed, window)
            work = sum((item[1] + item[2] for item in self.buckets if item[0] > int(now) - window), Fraction(0))
            try:
                rate = float(work * (2 ** 32)) / measured if measured else None
                if rate is not None and not math.isfinite(rate):
                    rate = None
            except OverflowError:
                rate = None
            result["hashrate_windows"][label] = {"hashrate_hs": rate, "measured_seconds": measured,
                                                  "warmup": elapsed < window}
        return result


class DeviceWork:
    """Accepted work per TCP session, in bounded one-minute buckets."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.started = clock()
        self.windows = {"15m": (900, deque()), "1h": (3600, deque()), "24h": (86400, deque())}
        self.totals = {label: Fraction(0) for label in self.windows}
        self.buckets = self.windows["24h"][1]

    def record(self, difficulty):
        minute = int(self.clock() // 60)
        if not self.buckets or self.buckets[-1][0] != minute:
            bucket = [minute, Fraction(0)]
            for _, buckets in self.windows.values():
                buckets.append(bucket)
        work = Fraction(str(difficulty))
        self.buckets[-1][1] += work
        for label in self.totals:
            self.totals[label] += work
        self._trim(minute)

    def _trim(self, minute):
        for label, (seconds, buckets) in self.windows.items():
            while buckets and buckets[0][0] <= minute - seconds // 60:
                self.totals[label] -= buckets.popleft()[1]

    def snapshot(self):
        now = self.clock()
        minute = int(now // 60)
        self._trim(minute)
        result = {}
        for label, (seconds, _) in self.windows.items():
            measured = min(max(0, now - self.started), seconds)
            work = self.totals[label]
            try:
                rate = float(work * 2 ** 32) / measured if measured else None
                if rate is not None and not math.isfinite(rate):
                    rate = None
            except OverflowError:
                rate = None
            result[label] = {"hashrate_hs": rate, "measured_seconds": measured,
                             "warmup": measured < seconds, "bucket_seconds": 60}
        return result


class WorkerRegistry:
    """Bounded process-local history keyed by exact worker name and source IP."""

    def __init__(self, max_workers=512, retention=86400, clock=time.monotonic):
        self.max_workers, self.retention, self.clock = max_workers, retention, clock
        self.entries = OrderedDict()
        self.sessions = {}
        self.dropped_observations = 0
        self.persistence = {"enabled": False, "loaded": False, "last_saved_utc": None, "error": None}

    @staticmethod
    def normalize_ip(value):
        address = ipaddress.ip_address(value.split("%", 1)[0])
        return str(address.ipv4_mapped or address) if isinstance(address, ipaddress.IPv6Address) else str(address)

    def _prune(self):
        now = self.clock()
        for key, row in list(self.entries.items()):
            if not row["sessions"] and now - row["touched"] >= self.retention:
                self.entries.pop(key)

    def authorize(self, session_id, name, ip):
        self._prune()
        key = (name, self.normalize_ip(ip))
        if key not in self.entries:
            if len(self.entries) >= self.max_workers:
                evict = next((k for k, row in self.entries.items() if not row["sessions"]), None)
                if evict is None:
                    self.dropped_observations += 1
                    return
                self.entries.pop(evict)
            self.entries[key] = {
                "id": hashlib.sha256((name + "\0" + key[1]).encode()).hexdigest()[:24],
                "worker": "".join(c if c.isprintable() else "?" for c in name),
                "ip": key[1], "sessions": set(), "accepted": 0, "rejected": 0,
                "stale": 0, "invalid_local": 0, "unknown": 0,
                "rtt_total_ms": 0.0, "rtt_samples": 0, "work": DeviceWork(self.clock),
                "first_share_ema_ms": None, "first_share_samples": 0,
                "difficulty_known": False, "touched": self.clock(), "last_seen_utc": None}
        self.entries[key]["sessions"].add(session_id)
        self.sessions.setdefault(session_id, set()).add(key)
        self._touch(key)

    def _touch(self, key):
        row = self.entries[key]
        row["touched"] = self.clock()
        row["last_seen_utc"] = datetime.now(timezone.utc).isoformat()
        self.entries.move_to_end(key)

    def record(self, session_id, name, outcome, difficulty=None, rtt_ms=None, stale=False):
        key = next((k for k in self.sessions.get(session_id, ()) if k[0] == name), None)
        if key is None or key not in self.entries:
            self.dropped_observations += 1
            return
        row = self.entries[key]
        row[outcome] += 1
        if stale and outcome == "rejected":
            row["stale"] += 1
        if difficulty is not None:
            row["difficulty_known"] = True
            if outcome == "accepted":
                row["work"].record(difficulty)
        if rtt_ms is not None and math.isfinite(rtt_ms) and rtt_ms >= 0:
            row["rtt_samples"] += 1
            row["rtt_total_ms"] += rtt_ms
        self._touch(key)

    def disconnect(self, session_id):
        for key in self.sessions.pop(session_id, ()):
            if key in self.entries:
                self.entries[key]["sessions"].discard(session_id)
                self._touch(key)
        self._prune()

    def first_share(self, session_id, name, milliseconds):
        if not math.isfinite(milliseconds) or milliseconds < 0:
            return
        key = next((key for key in self.sessions.get(session_id, ()) if key[0] == name), None)
        if key is not None and key in self.entries:
            row = self.entries[key]
            previous = row["first_share_ema_ms"]
            row["first_share_ema_ms"] = milliseconds if previous is None else .2 * milliseconds + .8 * previous
            row["first_share_samples"] += 1

    def snapshot(self):
        self._prune()
        rows = []
        for row in reversed(self.entries.values()):
            result = {key: row[key] for key in ("id", "worker", "ip", "accepted", "rejected", "stale",
                                                "invalid_local", "unknown", "last_seen_utc")}
            result.update(status="online" if row["sessions"] else "offline",
                          connection_count=len(row["sessions"]),
                          hashrate_windows=row["work"].snapshot() if row["difficulty_known"] else None,
                          pool_response_ms=row["rtt_total_ms"] / row["rtt_samples"] if row["rtt_samples"] else None,
                          asic_ping_ms=None, stale_basis="pool error code 21; subset of rejected",
                          notify_to_first_share_ema_ms=row["first_share_ema_ms"],
                          notify_to_first_share_samples=row["first_share_samples"],
                          latency_basis="notify queued to first pool-accepted submit received; EMA alpha=0.2; not TCP RTT",
                          scope="worker name + source IP; can combine multiple physical devices")
            rows.append(result)
        return {"workers": rows, "limit": self.max_workers,
                "retention_seconds": self.retention, "dropped_observations": self.dropped_observations,
                "resets_on_restart": not self.persistence["enabled"],
                "persistence": dict(self.persistence)}

    def dump_history(self, wall_now=None):
        self._prune()
        wall_now = time.time() if wall_now is None else wall_now
        mono = self.clock()
        rows = []
        for (name, ip), row in self.entries.items():
            values = {key: row[key] for key in ("accepted", "rejected", "stale", "invalid_local", "unknown",
                "rtt_total_ms", "rtt_samples", "difficulty_known", "last_seen_utc",
                "first_share_ema_ms", "first_share_samples")}
            values.update(name=name, ip=ip, touched_utc=wall_now - (mono - row["touched"]),
                          started_utc=wall_now - (mono - row["work"].started),
                          buckets=[[wall_now - (mono - minute * 60), str(work)]
                                   for minute, work in row["work"].buckets])
            rows.append(values)
        return {"version": 1, "saved_utc": wall_now, "workers": rows}

    def restore_history(self, data, wall_now=None):
        wall_now = time.time() if wall_now is None else wall_now
        if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("workers"), list):
            raise ValueError("unsupported history schema")
        if len(data["workers"]) > self.max_workers:
            raise ValueError("history worker limit exceeded")
        restored = WorkerRegistry(self.max_workers, self.retention, self.clock)
        mono = self.clock()
        for saved in data["workers"]:
            name, ip = saved["name"], saved["ip"]
            if not isinstance(name, str) or len(name) > 128 or not isinstance(ip, str):
                raise ValueError("invalid history identity")
            touched = float(saved["touched_utc"])
            started = float(saved["started_utc"])
            if not math.isfinite(touched) or not math.isfinite(started) or max(touched, started) > wall_now + 300:
                raise ValueError("invalid history timestamp")
            if wall_now - touched >= self.retention:
                continue
            if (name, restored.normalize_ip(ip)) in restored.entries:
                raise ValueError("duplicate history identity")
            restored.authorize(0, name, ip)
            row = restored.entries[(name, restored.normalize_ip(ip))]
            for key in ("accepted", "rejected", "stale", "invalid_local", "unknown", "rtt_samples", "first_share_samples"):
                value = saved[key]
                if type(value) is not int or not 0 <= value <= 2**63 - 1:
                    raise ValueError("invalid history counter")
                row[key] = value
            for key in ("rtt_total_ms", "first_share_ema_ms"):
                value = saved[key]
                if key == "rtt_total_ms" and value is None:
                    raise ValueError("invalid history latency")
                if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or value < 0):
                    raise ValueError("invalid history latency")
                row[key] = value
            row["difficulty_known"] = saved["difficulty_known"] is True
            row["last_seen_utc"] = saved["last_seen_utc"]
            row["touched"] = mono - max(0, wall_now - touched)
            row["sessions"].clear()
            work = row["work"]
            work.started = mono - min(86400, max(0, wall_now - started))
            buckets = saved["buckets"]
            if not isinstance(buckets, list) or len(buckets) > 1440:
                raise ValueError("history bucket limit exceeded")
            previous = None
            for utc, value in buckets:
                if type(utc) not in (int, float) or not math.isfinite(utc) or utc > wall_now + 60:
                    raise ValueError("invalid bucket timestamp")
                if wall_now - utc >= 86460:
                    continue
                if not isinstance(value, str) or len(value) > 4096:
                    raise ValueError("invalid bucket work")
                amount = Fraction(value)
                if amount < 0 or max(amount.numerator.bit_length(), amount.denominator.bit_length()) > 4096:
                    raise ValueError("invalid bucket work")
                minute = min(int(mono // 60), int((mono - (wall_now - utc)) // 60))
                if previous is not None and minute < previous:
                    raise ValueError("unordered history")
                if previous == minute:
                    bucket[1] += amount
                else:
                    bucket = [minute, amount]
                    for _, window in work.windows.values():
                        window.append(bucket)
                for label in work.totals:
                    work.totals[label] += amount
                previous = minute
            work._trim(int(mono // 60))
        self.entries = restored.entries
        self.sessions.clear()
        self.persistence["loaded"] = True


class Metrics:
    def __init__(self, parent=None):
        self._parent = parent
        self.active = 0
        self.connections = 0
        self.refused_capacity = 0
        self.refused_network = 0
        self.idle_timeouts = 0
        self.blocked_loops = 0
        self.backup_connections = 0
        self.closed_by_miner = 0
        self.closed_by_pool = 0
        self.closed_ambiguous = 0
        self.transport_errors = 0
        self.redirect_notices = 0
        self.share_rtt_samples = 0
        self.share_rtt_total_ms = 0.0
        self.upstream_failures = 0
        self.submitted = 0
        self.accepted = 0
        self.rejected = 0
        self.unmatched = 0
        self.observation_errors = 0
        self.invalid_local = 0
        self.duplicate_local = 0
        self.low_diff_local = 0
        self.unknown_job_local = 0
        self.malformed_local = 0
        self.protocol_drops = 0
        self.handshake_timeouts = 0
        self.zombie_reaped = 0
        self.keepalive_unavailable = 0
        self.compatibility_fallbacks = 0
        self.backpressure_pauses = 0
        self.write_timeouts = 0
        self.write_buffer_rejected = 0
        self.clean_jobs_coalesced = 0
        self.coalesced_read_batches = 0
        self.coalesced_extra_messages = 0

    def snapshot(self):
        result = {key: value for key, value in vars(self).items() if not key.startswith("_")}
        if getattr(self, "_log_handler", None) is not None:
            result["logging_dropped"] = self._log_handler.dropped
        if getattr(self, "_audit", None) is not None:
            result["audit"] = self._audit.snapshot()
        return result

    def __setattr__(self, key, value):
        parent = self.__dict__.get("_parent")
        if parent is not None and not key.startswith("_") and key in self.__dict__:
            setattr(parent, key, getattr(parent, key) + value - self.__dict__[key])
        object.__setattr__(self, key, value)


class Observer:
    MAX_LINE = 65536
    MAX_PENDING = 1024

    def __init__(self, metrics, on_authorized=None, clock=time.monotonic, on_share=None):
        self.metrics = metrics
        self.on_authorized = on_authorized
        self.clock = clock
        self.on_share = on_share
        self.buffers = [bytearray(), bytearray()]
        self.discard = [False, False]
        self.pending = OrderedDict()
        # Observations are informational only: never use them for routing or payouts.
        self.protocol = {"configured": False, "version_rolling": None, "version_mask": None,
                         "subscribed": False, "extranonce1": None, "extranonce2_size": None,
                         "extranonce_subscribed": False, "authorized_workers": [],
                         "observed_difficulty": None}

    @staticmethod
    def _hex(value, max_length, allow_empty=False):
        return (isinstance(value, str) and (bool(value) or allow_empty) and len(value) <= max_length
                and len(value) % 2 == 0 and all(c in string.hexdigits for c in value))

    def _extranonce(self, extra, size):
        if self._hex(extra, 64, allow_empty=True) and type(size) is int and 0 <= size <= 32:
            self.protocol["extranonce1"] = extra
            self.protocol["extranonce2_size"] = size
            return True
        return False

    def feed(self, direction, data):
        # Bound memory even when a peer sends unframed or oversized messages.
        for fragment in data.splitlines(keepends=True):
            end = fragment.endswith(b"\n")
            if not self.discard[direction]:
                buf = self.buffers[direction]
                if len(buf) + len(fragment) > self.MAX_LINE:
                    buf.clear()
                    self.discard[direction] = True
                    self.metrics.observation_errors += 1
                else:
                    buf.extend(fragment)
                    if end:
                        self._line(direction, bytes(buf))
                        buf.clear()
            if end:
                self.discard[direction] = False

    def _line(self, direction, line):
        try:
            msg = json.loads(line)
            if not isinstance(msg, dict):
                return
            if direction == 1 and msg.get("method") == "client.reconnect":
                self.metrics.redirect_notices += 1
            if direction == 1 and msg.get("method") == "mining.set_difficulty":
                params = msg.get("params")
                if isinstance(params, list) and params and type(params[0]) in (int, float):
                    value = params[0]
                    if value > 0 and math.isfinite(value):
                        self.protocol["observed_difficulty"] = value
            if direction == 1 and msg.get("method") == "mining.set_extranonce":
                params = msg.get("params")
                if isinstance(params, list) and len(params) >= 2:
                    self._extranonce(params[0], params[1])
            ident = msg.get("id")
            if not isinstance(ident, (int, str)) or isinstance(ident, bool):
                return
            if (isinstance(ident, str) and len(ident) > 128) or (isinstance(ident, int) and ident.bit_length() > 256):
                if direction == 0 and msg.get("method") == "mining.submit":
                    self.metrics.submitted += 1
                    self.metrics.unmatched += 1
                self.metrics.observation_errors += 1
                return
            if direction == 0 and isinstance(msg.get("method"), str):
                is_share = msg["method"] == "mining.submit"
                params = msg.get("params")
                worker = params[0] if isinstance(params, list) and params and isinstance(params[0], str) else None
                if is_share:
                    self.metrics.submitted += 1
                # Reused request IDs make both replies ambiguous.
                if ident in self.pending:
                    previous, payload = self.pending.pop(ident)
                    self._unknown(previous, payload)
                    if is_share:
                        self._unknown("share", (self.clock(), worker))
                    self.metrics.unmatched += int(previous == "share") + int(is_share)
                    self.pending[ident] = ("ambiguous", None)
                else:
                    kind = "share" if is_share else "other"
                    name = (self.clock(), worker) if is_share else None
                    if msg["method"] == "mining.authorize" and isinstance(params, list) and params and isinstance(params[0], str):
                        kind = "authorize"
                        name = params[0][:128]
                    elif msg["method"] in ("mining.configure", "mining.subscribe", "mining.extranonce.subscribe"):
                        kind = msg["method"]
                    self.pending[ident] = (kind, name)
                if len(self.pending) > self.MAX_PENDING:
                    _, (kind, payload) = self.pending.popitem(last=False)
                    self._unknown(kind, payload)
                    self.metrics.unmatched += int(kind == "share")
            elif direction == 1 and "method" not in msg and ident in self.pending:
                kind, name = self.pending[ident]
                # Once an ID is ambiguous, later replies cannot safely identify a request.
                if kind == "ambiguous":
                    return
                self.pending.pop(ident)
                if msg.get("error") is None:
                    result = msg.get("result")
                    if kind == "mining.configure" and isinstance(result, dict):
                        self.protocol["configured"] = True
                        rolling = result.get("version-rolling")
                        self.protocol["version_rolling"] = rolling if type(rolling) is bool else None
                        mask = result.get("version-rolling.mask")
                        self.protocol["version_mask"] = mask if self._hex(mask, 8) and len(mask) == 8 else None
                    elif kind == "mining.subscribe" and isinstance(result, list) and len(result) >= 3:
                        self.protocol["subscribed"] = self._extranonce(result[1], result[2])
                    elif kind == "mining.extranonce.subscribe" and result is True:
                        self.protocol["extranonce_subscribed"] = True
                if kind == "authorize" and msg.get("result") is True and msg.get("error") is None and self.on_authorized:
                    self.on_authorized(name)
                if kind == "authorize" and msg.get("result") is True and msg.get("error") is None:
                    workers = self.protocol["authorized_workers"]
                    if name not in workers and len(workers) < 32:
                        workers.append(name)
                if kind != "share":
                    return
                if msg.get("result") is True and msg.get("error") is None:
                    self.metrics.accepted += 1
                elif msg.get("result") is False or msg.get("error") is not None:
                    self.metrics.rejected += 1
                else:
                    self.metrics.unmatched += 1
                    self._unknown(kind, name)
                    return
                elapsed = max(0.0, (self.clock() - name[0]) * 1000)
                self.metrics.share_rtt_total_ms += elapsed
                self.metrics.share_rtt_samples += 1
                if self.on_share:
                    error = msg.get("error")
                    self.on_share(name[1], "accepted" if msg.get("result") is True and error is None else "rejected",
                                  elapsed, isinstance(error, list) and bool(error) and error[0] == 21)
        except (ValueError, UnicodeDecodeError, TypeError, RecursionError, OverflowError):
            self.metrics.observation_errors += 1

    def close(self):
        self.metrics.unmatched += sum(kind == "share" for kind, _ in self.pending.values())
        for kind, payload in self.pending.values():
            self._unknown(kind, payload)
        self.pending.clear()

    def _unknown(self, kind, payload):
        if self.on_share and kind == "share" and payload and payload[1] is not None:
            self.on_share(payload[1], "unknown", None, False)

    def pending_shares(self):
        return sum(kind == "share" for kind, _ in self.pending.values())
