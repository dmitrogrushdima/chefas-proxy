"""Local read-only dashboard HTTP handler and static assets."""
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path


class DashboardStream:
    """One producer, bounded latest-state queue per browser; no event replay."""

    def __init__(self, snapshot, interval=1.0, max_clients=8, drain_timeout=3.0):
        self.snapshot = snapshot
        self.interval, self.max_clients, self.drain_timeout = interval, max_clients, drain_timeout
        self.queues = set()
        self.handlers = set()
        self.producer = None
        self.sequence = 0
        self.closed = False

    async def _produce(self):
        while True:
            self.sequence += 1
            try:
                payload = json.dumps(self.snapshot(), allow_nan=False, separators=(",", ":")).encode()
                if len(payload) > 2 * 1024 * 1024:
                    raise ValueError("snapshot exceeds stream limit")
                frame = b"id: " + str(self.sequence).encode() + b"\nevent: snapshot\ndata: " + payload + b"\n\n"
            except Exception:
                frame = b"event: stream-error\ndata: {}\n\n"
            for queue in tuple(self.queues):
                if queue.full():
                    queue.get_nowait()
                queue.put_nowait(frame)
            await asyncio.sleep(self.interval)

    async def serve(self, reader, writer, allowed=None):
        if self.closed or len(self.queues) >= self.max_clients:
            writer.write(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nRetry-After: 5\r\nConnection: close\r\n\r\n")
            await asyncio.wait_for(writer.drain(), self.drain_timeout)
            return
        queue = asyncio.Queue(maxsize=1)
        self.queues.add(queue)
        self.handlers.add(asyncio.current_task())
        if self.producer is None:
            self.producer = asyncio.create_task(self._produce())
        disconnected = asyncio.create_task(reader.read(1))
        waiting = None
        try:
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\nX-Accel-Buffering: no\r\nConnection: keep-alive\r\n\r\n")
            await asyncio.wait_for(writer.drain(), self.drain_timeout)
            while True:
                waiting = asyncio.create_task(queue.get())
                done, _ = await asyncio.wait((waiting, disconnected), return_when=asyncio.FIRST_COMPLETED)
                if disconnected in done:
                    return
                if allowed is not None and not allowed():
                    return
                writer.write(waiting.result())
                await asyncio.wait_for(writer.drain(), self.drain_timeout)
        finally:
            for task in (waiting, disconnected):
                if task:
                    task.cancel()
            await asyncio.gather(*(task for task in (waiting, disconnected) if task), return_exceptions=True)
            self.queues.discard(queue)
            self.handlers.discard(asyncio.current_task())
            if not self.queues and self.producer is not None:
                producer = self.producer
                self.producer = None
                producer.cancel()
                await asyncio.gather(producer, return_exceptions=True)

    async def close(self):
        self.closed = True
        handlers = list(self.handlers)
        for task in handlers:
            task.cancel()
        await asyncio.gather(*handlers, return_exceptions=True)
        if self.producer is not None:
            self.producer.cancel()
            await asyncio.gather(self.producer, return_exceptions=True)
            self.producer = None


async def dashboard_request(reader, writer, metrics, sessions=lambda: [], status_snapshot=lambda: {},
                            stream=None, control=None, prometheus=None):
    try:
        disposition = b""
        request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 3)
        first = request.split(b"\r\n", 1)[0].split()
        # An absolute URL or another Host can indicate a cross-site request.
        headers = request.lower().split(b"\r\n")
        hosts = [h for h in headers if h.startswith(b"host:")]
        sock = writer.get_extra_info("sockname")
        expected_host = ("host: 127.0.0.1:" + str(sock[1])).encode()
        origins = [h for h in headers if h.startswith(b"origin:")]
        expected_origin = b"origin: http://127.0.0.1:" + str(sock[1]).encode()
        # A link from installation docs may open the public HTML shell. This
        # exception is only top-level navigation; APIs, frames and mutations
        # retain the cross-site denial and normal authentication checks.
        shell_navigation = (len(first) == 3 and first[0] == b"GET"
                            and first[1] in (b"/", b"/index.html")
                            and b"sec-fetch-mode: navigate" in headers
                            and b"sec-fetch-dest: document" in headers)
        if (len(request) > 8192 or len(first) != 3 or (first[0] != b"GET" and control is None) or hosts != [expected_host]
                or origins and origins != [expected_origin]
                or b"sec-fetch-site: cross-site" in headers and not shell_navigation):
            status, body, content_type = b"403 Forbidden", b"Forbidden", b"text/plain"
        elif control is not None and await control.handle(reader, writer, first[0].decode("ascii"), first[1].decode("ascii"), request.split(b"\r\n")[1:-2]):
            return
        elif first[0] != b"GET":
            status, body, content_type = b"403 Forbidden", b"Forbidden", b"text/plain"
        elif first[1] == b"/metrics" and prometheus is not None:
            status, body = b"200 OK", prometheus()
            content_type = b"text/plain; version=0.0.4; charset=utf-8"
        elif first[1] == b"/api/stream" and stream is not None:
            await stream.serve(reader, writer)
            return
        elif first[1] == b"/api/snapshot" and stream is not None:
            status, body, content_type = b"200 OK", json.dumps(stream.snapshot(), allow_nan=False).encode(), b"application/json"
        elif first[1] == b"/api/workers" and stream is not None:
            status, body, content_type = b"200 OK", json.dumps(stream.snapshot()["registry"], allow_nan=False).encode(), b"application/json"
        elif first[1] == b"/api/metrics":
            status, body, content_type = b"200 OK", json.dumps(metrics.snapshot()).encode(), b"application/json"
        elif first[1] == b"/api/sessions":
            status, body, content_type = b"200 OK", json.dumps(sessions()).encode(), b"application/json"
        elif first[1] == b"/api/status":
            status, body, content_type = b"200 OK", json.dumps(status_snapshot()).encode(), b"application/json"
        elif first[1] == b"/api/audit-export":
            allowed = ("id", "state", "worker", "pool", "duration_seconds", "submitted", "accepted", "rejected", "unmatched", "pending")
            safe_sessions = [{key: item.get(key) for key in allowed} for item in sessions()]
            report = {"schema_version": 1, "generated_at_utc": datetime.now(timezone.utc).isoformat(),
                      "scope": "approximate process-lifetime observations; not a financial audit",
                      "devfee_enabled": False, "accepted_difficulty": None, "fee_difficulty": None,
                      "fee_percentage": None, "metrics": metrics.snapshot(), "sessions": safe_sessions}
            ledger = getattr(metrics, "_ledger", None)
            if ledger is not None:
                tracker = getattr(metrics, "_audit", None)
                audit = tracker.snapshot() if tracker is not None else ledger.snapshot()
                report.update({"scope": ("pool-confirmed work restored from SQLite plus current observations"
                                         if getattr(metrics, "_devfee_journal", None) is not None
                                         else "pool-confirmed work since process start"),
                               "devfee_enabled": bool(getattr(metrics, "_devfee_enabled", False)),
                               "accepted_difficulty": audit["user_accepted_difficulty"],
                               "fee_difficulty": audit["fee_accepted_difficulty"],
                               "fee_percentage": audit["fee_percentage"], "audit": audit})
            journal = getattr(metrics, "_devfee_journal", None)
            if journal is not None:
                report["persistent_work"] = await journal.snapshot()
            status, body, content_type = b"200 OK", json.dumps(report).encode(), b"application/json"
            disposition = b"\r\nContent-Disposition: attachment; filename=chefas-proxy-diagnostics.json"
        elif first[1] in ASSETS:
            body, content_type = ASSETS[first[1]]
            status = b"200 OK"
        elif first[1] == b"/":
            status, body, content_type = b"200 OK", ASSETS[b"/index.html"][0], b"text/html; charset=utf-8"
        else:
            status, body, content_type = b"404 Not Found", b"Not found", b"text/plain"
        writer.write(b"HTTP/1.1 " + status + b"\r\nContent-Type: " + content_type +
                     b"\r\nCache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\nContent-Security-Policy: default-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'" + disposition + b"\r\nContent-Length: " +
                     str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
        await asyncio.wait_for(writer.drain(), 3)
    except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        pass
    finally:
        writer.close()
        try:
            await asyncio.wait_for(writer.wait_closed(), 3)
        except (OSError, asyncio.TimeoutError):
            pass


ASSET_DIR = Path(__file__).resolve().parent / "dashboard"
if not ASSET_DIR.is_dir():
    ASSET_DIR = Path(__file__).resolve().parent.parent / "dashboard"
ASSETS = {b"/index.html": ((ASSET_DIR / "index.html").read_bytes(), b"text/html; charset=utf-8"),
          b"/app.css": ((ASSET_DIR / "app.css").read_bytes(), b"text/css; charset=utf-8"),
          b"/i18n.js": ((ASSET_DIR / "i18n.js").read_bytes(), b"application/javascript; charset=utf-8"),
          b"/app.js": ((ASSET_DIR / "app.js").read_bytes(), b"application/javascript; charset=utf-8")}
