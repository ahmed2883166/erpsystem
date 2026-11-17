import sqlite3
import tempfile
import unittest
from pathlib import Path

from sqlite_maintenance import (
    enable_sqlite_wal,
    online_backup_sqlite,
    restore_sqlite_snapshot,
    sqlite_integrity_check,
    sqlite_table_names,
)


class SQLiteMaintenanceTests(unittest.TestCase):
    def test_wal_setup_creates_parent_and_enables_persistent_mode(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / "nested" / "nexus.db"
            self.assertEqual(enable_sqlite_wal(database), "wal")
            connection = sqlite3.connect(database)
            try:
                self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
            finally:
                connection.close()

    def test_online_backup_preserves_data_and_checks_integrity(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.db"
            snapshot = root / "snapshots" / "snapshot.db"
            connection = sqlite3.connect(source)
            connection.execute("CREATE TABLE sample(value TEXT NOT NULL)")
            connection.execute("INSERT INTO sample VALUES('preserved')")
            connection.commit()
            connection.close()

            self.assertEqual(online_backup_sqlite(source, snapshot), snapshot)
            copied = sqlite3.connect(snapshot)
            try:
                self.assertEqual(copied.execute("SELECT value FROM sample").fetchone()[0], "preserved")
                self.assertEqual(sqlite_integrity_check(copied), "ok")
                self.assertIn("sample", sqlite_table_names(copied))
            finally:
                copied.close()

    def test_restore_snapshot_replaces_database_contents(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "candidate.db"
            live = root / "live.db"
            for path, value in ((source, "new"), (live, "old")):
                connection = sqlite3.connect(path)
                connection.execute("CREATE TABLE sample(value TEXT NOT NULL)")
                connection.execute("INSERT INTO sample VALUES(?)", (value,))
                connection.commit()
                connection.close()

            self.assertTrue(restore_sqlite_snapshot(source, live))
            restored = sqlite3.connect(live)
            try:
                self.assertEqual(restored.execute("SELECT value FROM sample").fetchone()[0], "new")
            finally:
                restored.close()


if __name__ == "__main__":
    unittest.main()
