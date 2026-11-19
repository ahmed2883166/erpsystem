import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as nexus
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


class SessionRevocationRouteTests(unittest.TestCase):
    def test_old_authenticated_cookie_is_redirected_to_login(self):
        with tempfile.TemporaryDirectory() as folder:
            epoch_path = Path(folder) / ".session_epoch"
            current_session_epoch(epoch_path)
            with patch.object(nexus, "SESSION_EPOCH_PATH", epoch_path):
                nexus.app.config.update(TESTING=True)
                client = nexus.app.test_client()
                with client.session_transaction() as session:
                    session.update(user_id=999, session_epoch="old-epoch", role="admin")
                response = client.get("/dashboard")
                self.assertEqual(response.status_code, 302)
                self.assertTrue(response.headers["Location"].endswith("/login"))
                with client.session_transaction() as session:
                    self.assertNotIn("user_id", session)


if __name__ == "__main__":
    unittest.main()
