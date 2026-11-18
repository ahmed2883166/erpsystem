import os
import subprocess
import sys
import tempfile
import unittest


class LANStartupGuardTests(unittest.TestCase):
    def _run_app_with_environment(self, data_dir, *, allow_lan=None,
                                  cookie_secure=None, skip_db_init="1"):
        env = os.environ.copy()
        env["NEXUS_DATA_DIR"] = os.path.join(data_dir, "data")
        env["NEXUS_BACKUP_DIR"] = os.path.join(data_dir, "backups")
        env["NEXUS_DEMO_MODE"] = "1"
        env["HOST"] = "0.0.0.0"
        env["PORT"] = "5055"
        env["NEXUS_SKIP_DB_INIT"] = skip_db_init
        env.pop("NEXUS_ALLOW_LAN", None)
        env.pop("NEXUS_COOKIE_SECURE", None)
        if allow_lan is not None:
            env["NEXUS_ALLOW_LAN"] = allow_lan
        if cookie_secure is not None:
            env["NEXUS_COOKIE_SECURE"] = cookie_secure
        return subprocess.run(
            [sys.executable, "app.py"],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_lan_binding_requires_explicit_opt_in(self):
        with tempfile.TemporaryDirectory(prefix="nexus-lan-opt-in-") as data_dir:
            result = self._run_app_with_environment(data_dir)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("LAN binding is disabled by default", result.stderr)

    def test_lan_binding_requires_secure_session_cookies(self):
        with tempfile.TemporaryDirectory(prefix="nexus-lan-secure-cookie-") as data_dir:
            result = self._run_app_with_environment(
                data_dir, allow_lan="1", cookie_secure="0"
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("LAN binding requires NEXUS_COOKIE_SECURE=1", result.stderr)

    def test_lan_binding_requires_initial_admin_password_change(self):
        with tempfile.TemporaryDirectory(prefix="nexus-lan-admin-password-") as data_dir:
            result = self._run_app_with_environment(
                data_dir, allow_lan="1", cookie_secure="1", skip_db_init="0"
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "Complete the administrator's required password change locally",
            result.stderr,
        )


if __name__ == "__main__":
    unittest.main()
