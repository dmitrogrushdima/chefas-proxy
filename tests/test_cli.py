import subprocess
import sys
import unittest
from pathlib import Path


PROXY = str(Path(__file__).resolve().parent.parent / "launch.py")


class CLITests(unittest.TestCase):
    def test_rejects_invalid_configuration_before_opening_sockets(self):
        for extra in (["--backup-host", "example.invalid"],
                      ["--max-connections", "0"],
                      ["--connect-timeout", "nan"],
                      ["--connect-timeout", "0"],
                      ["--idle-timeout", "nan"],
                      ["--idle-timeout", "-1"],
                      ["--miner-timeout", "nan"],
                      ["--miner-timeout", "-1"],
                      ["--handshake-timeout", "0"],
                      ["--handshake-timeout", "inf"],
                      ["--max-miner-message", "511"],
                      ["--max-miner-message", "65537"],
                      ['--devfee-jitter-seconds','nan'],
                      ['--devfee-connect-interval','-1'],
                      ['--job-ttl','0'],
                      ['--job-gc-interval','inf'],
                      ['--devfee-worker-template','{worker.__class__}'],
                      ["--allow-cidr", "192.168.0.1/24"],
                      ["--allow-cidr", "invalid"],
                      ["--listen-host", "0.0.0.0"],
                      ["--allow-cidr", "127.0.0.0/8", "--allow-any-miner"]):
            with self.subTest(extra=extra):
                result = subprocess.run([sys.executable, PROXY, "--pool-host", "example.invalid",
                                         "--pool-port", "3333", *extra],
                                        capture_output=True, timeout=5)
                self.assertEqual(result.returncode, 2)
