"""Best-effort, passive Stratum V1 observations; never controls forwarding."""
import json
from collections import OrderedDict


class Metrics:
    def __init__(self):
        self.active = 0
        self.connections = 0
        self.upstream_failures = 0
        self.submitted = 0
        self.accepted = 0
        self.rejected = 0
        self.unmatched = 0
        self.observation_errors = 0

    def snapshot(self):
        return dict(vars(self))


class Observer:
    MAX_LINE = 65536
    MAX_PENDING = 1024

    def __init__(self, metrics):
        self.metrics = metrics
        self.buffers = [bytearray(), bytearray()]
        self.discard = [False, False]
        self.pending = OrderedDict()

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
            ident = msg.get("id")
            if not isinstance(ident, (int, str)) or isinstance(ident, bool):
                return
            if direction == 0 and isinstance(msg.get("method"), str):
                is_share = msg["method"] == "mining.submit"
                if is_share:
                    self.metrics.submitted += 1
                # Reused request IDs make both replies ambiguous.
                if ident in self.pending:
                    previous = self.pending.pop(ident)
                    self.metrics.unmatched += int(previous == "share") + int(is_share)
                    self.pending[ident] = "ambiguous"
                else:
                    self.pending[ident] = "share" if is_share else "other"
                if len(self.pending) > self.MAX_PENDING:
                    _, kind = self.pending.popitem(last=False)
                    self.metrics.unmatched += int(kind == "share")
            elif direction == 1 and "method" not in msg and ident in self.pending:
                if self.pending.pop(ident) != "share":
                    return
                if msg.get("result") is True and msg.get("error") is None:
                    self.metrics.accepted += 1
                elif msg.get("result") is False or msg.get("error") is not None:
                    self.metrics.rejected += 1
                else:
                    self.metrics.unmatched += 1
        except (ValueError, UnicodeDecodeError, TypeError, RecursionError):
            self.metrics.observation_errors += 1

    def close(self):
        self.metrics.unmatched += sum(kind == "share" for kind in self.pending.values())
        self.pending.clear()
