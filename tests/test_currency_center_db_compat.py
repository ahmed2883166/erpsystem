"""Currency center database-neutral metadata helper tests."""
import os
import sqlite3
import unittest

import currency_center


class CurrencyMetadataSQLiteTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            "CREATE TABLE audit_log (id INTEGER PRIMARY KEY, action TEXT NOT NULL)"
        )

    def tearDown(self):
        self.conn.close()

    def test_optional_audit_table_detection(self):
        self.assertTrue(currency_center._table_exists(self.conn, "audit_log"))
        self.assertFalse(currency_center._table_exists(self.conn, "missing_audit_log"))


@unittest.skipUnless(
    os.environ.get("NEXUS_TEST_POSTGRES_DSN"),
    "PostgreSQL integration DSN not configured",
)
class CurrencyMetadataPostgreSQLTests(unittest.TestCase):
    def test_optional_audit_table_detection_in_rolled_back_schema(self):
        import uuid

        import psycopg
        from psycopg.rows import dict_row

        schema = "nexus_currency_meta_" + uuid.uuid4().hex[:12]
        conn = psycopg.connect(
            os.environ["NEXUS_TEST_POSTGRES_DSN"], row_factory=dict_row
        )
        try:
            try:
                with conn.transaction():
                    conn.execute(f'CREATE SCHEMA "{schema}"')
                    conn.execute(f'SET LOCAL search_path TO "{schema}"')
                    conn.execute(
                        "CREATE TABLE audit_log (id BIGINT PRIMARY KEY, action TEXT NOT NULL)"
                    )
                    self.assertTrue(currency_center._table_exists(conn, "audit_log"))
                    self.assertFalse(
                        currency_center._table_exists(conn, "missing_audit_log")
                    )
                    raise RuntimeError("rollback disposable schema")
            except RuntimeError as exc:
                self.assertEqual(str(exc), "rollback disposable schema")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
