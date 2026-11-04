import subprocess
import sys
import unittest
from pathlib import Path


PROXY = str(Path(__file__).resolve().parent.parent / "proxy.py")


class CLITests(unittest.TestCase):
    def test_rejects_invalid_configuration_before_opening_sockets(self):
        for extra in (["--backup-host", "example.invalid"],
                      ["--max-connections", "0"],
                      ["--connect-timeout", "nan"],
                      ["--connect-timeout", "0"]):
            with self.subTest(extra=extra):
                result = subprocess.run([sys.executable, PROXY, "--pool-host", "example.invalid",
                                         "--pool-port", "3333", *extra],
                                        capture_output=True, timeout=5)
                self.assertEqual(result.returncode, 2)
