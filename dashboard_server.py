"""Local read-only dashboard HTTP handler and static assets."""
import asyncio
import json
from pathlib import Path


async def dashboard_request(reader, writer, metrics, sessions=lambda: [], status_snapshot=lambda: {}):
    try:
        request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 3)
        first = request.split(b"\r\n", 1)[0].split()
        # An absolute URL or another Host can indicate a cross-site request.
        headers = request.lower().split(b"\r\n")
        hosts = [h for h in headers if h.startswith(b"host:")]
        sock = writer.get_extra_info("sockname")
        expected_host = ("host: 127.0.0.1:" + str(sock[1])).encode()
        if len(first) != 3 or first[0] != b"GET" or hosts != [expected_host]:
            status, body, content_type = b"403 Forbidden", b"Forbidden", b"text/plain"
        elif first[1] == b"/api/metrics":
            status, body, content_type = b"200 OK", json.dumps(metrics.snapshot()).encode(), b"application/json"
        elif first[1] == b"/api/sessions":
            status, body, content_type = b"200 OK", json.dumps(sessions()).encode(), b"application/json"
        elif first[1] == b"/api/status":
            status, body, content_type = b"200 OK", json.dumps(status_snapshot()).encode(), b"application/json"
        elif first[1] in ASSETS:
            body, content_type = ASSETS[first[1]]
            status = b"200 OK"
        elif first[1] == b"/":
            status, body, content_type = b"200 OK", ASSETS[b"/index.html"][0], b"text/html; charset=utf-8"
        else:
            status, body, content_type = b"404 Not Found", b"Not found", b"text/plain"
        writer.write(b"HTTP/1.1 " + status + b"\r\nContent-Type: " + content_type +
                     b"\r\nCache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\nContent-Security-Policy: default-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'\r\nContent-Length: " +
                     str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
        await writer.drain()
    except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        pass
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass


ASSET_DIR = Path(__file__).resolve().parent / "dashboard"
ASSETS = {b"/index.html": ((ASSET_DIR / "index.html").read_bytes(), b"text/html; charset=utf-8"),
          b"/app.css": ((ASSET_DIR / "app.css").read_bytes(), b"text/css; charset=utf-8"),
          b"/i18n.js": ((ASSET_DIR / "i18n.js").read_bytes(), b"application/javascript; charset=utf-8"),
          b"/app.js": ((ASSET_DIR / "app.js").read_bytes(), b"application/javascript; charset=utf-8")}
