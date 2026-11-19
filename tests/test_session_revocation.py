import tempfile
import unittest
from pathlib import Path

from session_revocation import current_session_epoch, rotate_session_epoch


class SessionRevocationTests(unittest.TestCase):
    def test_epoch_is_stable_until_rotated(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / ".session_epoch"
            first = current_session_epoch(path)
            self.assertTrue(first)
            self.assertEqual(current_session_epoch(path), first)
            second = rotate_session_epoch(path)
            self.assertNotEqual(second, first)
            self.assertEqual(current_session_epoch(path), second)


if __name__ == "__main__":
    unittest.main()
