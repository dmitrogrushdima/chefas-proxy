import unittest

from proxy import fd_limit_warning


class FileDescriptorLimitTests(unittest.TestCase):
    def test_insufficient_or_unknown_limit(self):
        self.assertIn("1024", fd_limit_warning(512, 1024))
        self.assertIsNone(fd_limit_warning(512, 2048))
        self.assertIsNone(fd_limit_warning(512, -1))
