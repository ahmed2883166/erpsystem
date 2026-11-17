import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from database import connect_database, enable_wal, fetch_page
from app_paths import ensure_data_directory


POSTGRES_DSN = os.environ.get("NEXUS_TEST_POSTGRES_DSN", "").strip()


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

    def test_fetch_page_limits_rows_and_normalizes_page_size(self):
        with tempfile.TemporaryDirectory() as directory:
            conn = connect_database(Path(directory) / "page.db", isolation_level=None)
            try:
                conn.execute("CREATE TABLE sample(id INTEGER PRIMARY KEY)")
                conn.executemany("INSERT INTO sample(id) VALUES(?)", [(i,) for i in range(1, 126)])
                result = fetch_page(
                    conn, "SELECT id FROM sample ORDER BY id",
                    "SELECT COUNT(*) FROM sample", page=2, per_page=50,
                )
                self.assertEqual(result["total"], 125)
                self.assertEqual(result["pages"], 3)
                self.assertEqual(result["page"], 2)
                self.assertEqual(result["per_page"], 50)
                self.assertEqual([row["id"] for row in result["rows"]], list(range(51, 101)))
                fallback = fetch_page(
                    conn, "SELECT id FROM sample ORDER BY id",
                    "SELECT COUNT(*) FROM sample", page=999, per_page=75,
                )
                self.assertEqual(fallback["page"], 3)
                self.assertEqual(fallback["per_page"], 50)
                self.assertEqual(len(fallback["rows"]), 25)
            finally:
                conn.close()

    def test_local_launcher_does_not_inherit_database_init_skip(self):
        launcher = Path(__file__).resolve().parents[1] / "run_local.bat"
        content = launcher.read_text(encoding="utf-8")
        self.assertIn('set "NEXUS_SKIP_DB_INIT=0"', content)

    def test_data_directory_creates_missing_parent_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "nested" / "runtime" / "data"
            created = ensure_data_directory(target)
            self.assertEqual(created, target.resolve())
            self.assertTrue(created.is_dir())

    def test_wal_is_enabled_and_persistent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nexus.db"
            self.assertEqual(enable_wal(path), "wal")
            conn = sqlite3.connect(path)
            try:
                self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
            finally:
                conn.close()



@unittest.skipUnless(POSTGRES_DSN, "NEXUS_TEST_POSTGRES_DSN is not configured")
class PostgreSQLPaginationTests(unittest.TestCase):
    def test_fetch_page_supports_dict_rows_and_postgres_placeholders(self):
        from postgres_database import connect_postgres

        connection = connect_postgres(POSTGRES_DSN)
        try:
            connection.execute(
                "CREATE TEMP TABLE pagination_probe (id BIGINT PRIMARY KEY, group_id BIGINT NOT NULL)"
            )
            connection.executemany(
                "INSERT INTO pagination_probe(id, group_id) VALUES (%s, %s)",
                [(value, 1 if value <= 75 else 2) for value in range(1, 126)],
            )
            result = fetch_page(
                connection,
                "SELECT id FROM pagination_probe WHERE group_id = ? ORDER BY id",
                "SELECT COUNT(*) AS total FROM pagination_probe WHERE group_id = ?",
                params=(2,),
                page=2,
                per_page=50,
            )
            self.assertEqual(result["total"], 50)
            self.assertEqual(result["pages"], 1)
            self.assertEqual(result["page"], 1)
            self.assertEqual([row["id"] for row in result["rows"]], list(range(76, 126)))
        finally:
            connection.rollback()
            connection.close()


if __name__ == "__main__":
    unittest.main()
