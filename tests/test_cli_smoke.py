import json
import socket
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def reserve_distinct_ports(count):
    sockets = []
    try:
        for _ in range(count):
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            sockets.append(sock)
        return [sock.getsockname()[1] for sock in sockets]
    finally:
        for sock in sockets:
            sock.close()


class CLISmokeTests(unittest.TestCase):
    def test_real_entrypoint_serves_dashboard(self):
        listener, dashboard, upstream = reserve_distinct_ports(3)
        process = subprocess.Popen([sys.executable, str(ROOT / "launch.py"), "--pool-host", "127.0.0.1",
                                    "--pool-port", str(upstream), "--listen-port", str(listener),
                                    "--dashboard-port", str(dashboard)], cwd=ROOT,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            url = f"http://127.0.0.1:{dashboard}"
            deadline = time.monotonic() + 8
            while True:
                if process.poll() is not None:
                    self.fail("Proxy exited unexpectedly: " + process.stderr.read().decode(errors="replace"))
                try:
                    with urllib.request.urlopen(url + "/api/status", timeout=1) as response:
                        state = json.load(response)
                    break
                except (urllib.error.URLError, TimeoutError):
                    if time.monotonic() >= deadline:
                        self.fail("Dashboard did not become ready")
                    time.sleep(.05)
            self.assertEqual(state["primary"]["port"], upstream)
            self.assertEqual(state["listeners"], [str(listener)])
            with urllib.request.urlopen(url + "/", timeout=2) as response:
                self.assertIn(b"/app.js", response.read())
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            process.stderr.close()
