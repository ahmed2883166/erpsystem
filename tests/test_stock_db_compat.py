"""Stock module database-neutral metadata helper tests."""
import os
import sqlite3
import unittest

from stock import _columns, _table_exists


class StockMetadataSQLiteTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("CREATE TABLE stock_probe (id INTEGER PRIMARY KEY, sku TEXT)")

    def tearDown(self):
        self.conn.close()

    def test_table_exists(self):
        self.assertTrue(_table_exists(self.conn, "stock_probe"))
        self.assertFalse(_table_exists(self.conn, "missing_probe"))

    def test_columns(self):
        self.assertEqual(_columns(self.conn, "stock_probe"), {"id", "sku"})
        self.assertEqual(_columns(self.conn, "missing_probe"), set())


@unittest.skipUnless(os.environ.get("NEXUS_TEST_POSTGRES_DSN"), "PostgreSQL integration DSN not configured")
class StockMetadataPostgreSQLTests(unittest.TestCase):
    def test_metadata_helpers_in_rolled_back_schema(self):
        import uuid
        import psycopg
        from psycopg.rows import dict_row

        schema = "nexus_stock_meta_" + uuid.uuid4().hex[:12]
        conn = psycopg.connect(os.environ["NEXUS_TEST_POSTGRES_DSN"], row_factory=dict_row)
        try:
            try:
                with conn.transaction():
                    conn.execute(f'CREATE SCHEMA "{schema}"')
                    conn.execute(f'SET LOCAL search_path TO "{schema}"')
                    conn.execute("CREATE TABLE stock_probe (id BIGINT PRIMARY KEY, sku TEXT)")
                    self.assertTrue(_table_exists(conn, "stock_probe"))
                    self.assertFalse(_table_exists(conn, "missing_probe"))
                    self.assertEqual(_columns(conn, "stock_probe"), {"id", "sku"})
                    self.assertEqual(_columns(conn, "missing_probe"), set())
                    raise RuntimeError("rollback disposable schema")
            except RuntimeError as exc:
                self.assertEqual(str(exc), "rollback disposable schema")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
