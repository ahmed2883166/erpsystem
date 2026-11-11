import sqlite3
import tempfile
import unittest
from pathlib import Path

from database import connect_database, enable_wal


class SharedDatabaseTests(unittest.TestCase):
    def test_shared_connection_applies_common_pragmas_and_decimal_aggregates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nexus.db"
            conn = connect_database(path, isolation_level=None)
            try:
                self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
                self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 20000)
                self.assertIsInstance(conn.execute("SELECT 1 AS value").fetchone(), sqlite3.Row)
                conn.execute("CREATE TABLE amounts(value TEXT NOT NULL)")
                conn.executemany("INSERT INTO amounts(value) VALUES(?)", [("0.10",), ("0.20",)])
                self.assertEqual(conn.execute("SELECT DECIMAL_SUM(value) FROM amounts").fetchone()[0], "0.30")
            finally:
                conn.close()

    def test_wal_is_enabled_and_persistent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nexus.db"
            self.assertEqual(enable_wal(path), "wal")
            conn = sqlite3.connect(path)
            try:
                self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
