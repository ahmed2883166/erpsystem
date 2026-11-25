import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_runtime_readiness.py"


class RuntimeReadinessPreflightTests(unittest.TestCase):
    def _run(self, root):
        data_dir = Path(root) / "data"
        backup_dir = Path(root) / "backups"
        backup_dir.mkdir()
        env = os.environ.copy()
        env["NEXUS_DATA_DIR"] = str(data_dir)
        env["NEXUS_BACKUP_DIR"] = str(backup_dir)
        return subprocess.run(
            [sys.executable, str(SCRIPT)],
            env=env, capture_output=True, text=True, timeout=30,
        )

    def test_fresh_install_preflight_does_not_create_company_database(self):
        with tempfile.TemporaryDirectory(prefix="nexus-preflight-fresh-") as root:
            result = self._run(root)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("not created yet", result.stdout)
            self.assertFalse((Path(root) / "data" / "nexus.db").exists())

    def test_existing_database_is_checked_read_only(self):
        with tempfile.TemporaryDirectory(prefix="nexus-preflight-existing-") as root:
            data_dir = Path(root) / "data"
            data_dir.mkdir()
            database = data_dir / "nexus.db"
            connection = sqlite3.connect(database)
            connection.execute("CREATE TABLE preserve_me(value TEXT)")
            connection.execute("INSERT INTO preserve_me VALUES('untouched')")
            connection.commit()
            connection.close()
            before = database.read_bytes()

            result = self._run(root)

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("integrity OK", result.stdout)
            self.assertEqual(database.read_bytes(), before)
            check = sqlite3.connect(database)
            try:
                self.assertEqual(
                    check.execute("SELECT value FROM preserve_me").fetchone()[0],
                    "untouched",
                )
            finally:
                check.close()

    def test_corrupt_database_is_reported_without_repairing_it(self):
        with tempfile.TemporaryDirectory(prefix="nexus-preflight-corrupt-") as root:
            data_dir = Path(root) / "data"
            data_dir.mkdir()
            database = data_dir / "nexus.db"
            database.write_bytes(b"not a sqlite database")
            before = database.read_bytes()

            result = self._run(root)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("could not be checked read-only", result.stdout)
            self.assertEqual(database.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
