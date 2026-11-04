"""Best-effort, passive Stratum V1 observations; never controls forwarding."""
import json
import time
import math
import string
from collections import OrderedDict


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

    def snapshot(self):
        return {key: value for key, value in vars(self).items() if not key.startswith("_")}

    def __setattr__(self, key, value):
        parent = self.__dict__.get("_parent")
        if parent is not None and not key.startswith("_") and key in self.__dict__:
            setattr(parent, key, getattr(parent, key) + value - self.__dict__[key])
        object.__setattr__(self, key, value)


class Observer:
    MAX_LINE = 65536
    MAX_PENDING = 1024

    def __init__(self, metrics, on_authorized=None, clock=time.monotonic):
        self.metrics = metrics
        self.on_authorized = on_authorized
        self.clock = clock
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
                if is_share:
                    self.metrics.submitted += 1
                # Reused request IDs make both replies ambiguous.
                if ident in self.pending:
                    previous, _ = self.pending.pop(ident)
                    self.metrics.unmatched += int(previous == "share") + int(is_share)
                    self.pending[ident] = ("ambiguous", None)
                else:
                    kind = "share" if is_share else "other"
                    name = self.clock() if is_share else None
                    params = msg.get("params")
                    if msg["method"] == "mining.authorize" and isinstance(params, list) and params and isinstance(params[0], str):
                        kind = "authorize"
                        name = params[0][:128]
                    elif msg["method"] in ("mining.configure", "mining.subscribe", "mining.extranonce.subscribe"):
                        kind = msg["method"]
                    self.pending[ident] = (kind, name)
                if len(self.pending) > self.MAX_PENDING:
                    _, (kind, _) = self.pending.popitem(last=False)
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
                    return
                elapsed = max(0.0, (self.clock() - name) * 1000)
                self.metrics.share_rtt_total_ms += elapsed
                self.metrics.share_rtt_samples += 1
        except (ValueError, UnicodeDecodeError, TypeError, RecursionError, OverflowError):
            self.metrics.observation_errors += 1

    def close(self):
        self.metrics.unmatched += sum(kind == "share" for kind, _ in self.pending.values())
        self.pending.clear()

    def pending_shares(self):
        return sum(kind == "share" for kind, _ in self.pending.values())
