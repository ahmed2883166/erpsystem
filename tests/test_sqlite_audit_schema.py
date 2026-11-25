import sqlite3
import unittest

from sqlite_audit_schema import initialize_sqlite_audit_schema


class SQLiteAuditSchemaTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("""
            CREATE TABLE audit_log (
                id INTEGER PRIMARY KEY,
                user_id INTEGER,
                action TEXT NOT NULL,
                details TEXT,
                created_at TEXT NOT NULL
            )
        """)

    def tearDown(self):
        self.connection.close()

    def test_adds_audit_columns_and_indexes_idempotently(self):
        self.connection.execute(
            "INSERT INTO audit_log(action,created_at) VALUES('login','2026-01-01')"
        )
        self.assertTrue(initialize_sqlite_audit_schema(self.connection))
        self.assertTrue(initialize_sqlite_audit_schema(self.connection))
        columns = {
            row[1] for row in self.connection.execute("PRAGMA table_info(audit_log)")
        }
        self.assertTrue({
            "entity_type", "entity_id", "remote_addr", "outcome", "actor_name",
            "before_values", "after_values", "submitted_values", "reason", "request_id",
        }.issubset(columns))
        row = self.connection.execute(
            "SELECT action,created_at FROM audit_log WHERE id=1"
        ).fetchone()
        self.assertEqual(row, ("login", "2026-01-01"))

    def test_audit_log_rejects_update_and_delete(self):
        initialize_sqlite_audit_schema(self.connection)
        self.connection.execute(
            "INSERT INTO audit_log(action,created_at) VALUES('login','2026-01-01')"
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
            self.connection.execute("UPDATE audit_log SET action='changed' WHERE id=1")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
            self.connection.execute("DELETE FROM audit_log WHERE id=1")


if __name__ == "__main__":
    unittest.main()
