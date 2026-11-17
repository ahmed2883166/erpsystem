"""Database-neutral account chart metadata inspection tests."""
import os
import sqlite3
import unittest

from chart_templates import _account_columns


class AccountChartMetadataSQLiteTests(unittest.TestCase):
    def test_account_columns_are_read_from_sqlite(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("CREATE TABLE accounts (id INTEGER PRIMARY KEY, code TEXT, name_ar TEXT)")
            self.assertEqual(_account_columns(conn), {"id", "code", "name_ar"})
        finally:
            conn.close()


@unittest.skipUnless(os.environ.get("NEXUS_TEST_POSTGRES_DSN"), "PostgreSQL integration DSN not configured")
class AccountChartMetadataPostgreSQLTests(unittest.TestCase):
    def test_account_columns_in_rolled_back_schema(self):
        import uuid
        import psycopg
        from psycopg.rows import dict_row

        schema = "nexus_chart_meta_" + uuid.uuid4().hex[:12]
        conn = psycopg.connect(os.environ["NEXUS_TEST_POSTGRES_DSN"], row_factory=dict_row)
        try:
            try:
                with conn.transaction():
                    conn.execute(f'CREATE SCHEMA "{schema}"')
                    conn.execute(f'SET LOCAL search_path TO "{schema}"')
                    conn.execute("CREATE TABLE accounts (id BIGINT PRIMARY KEY, code TEXT, name_ar TEXT)")
                    self.assertEqual(_account_columns(conn), {"id", "code", "name_ar"})
                    raise RuntimeError("rollback disposable schema")
            except RuntimeError as exc:
                self.assertEqual(str(exc), "rollback disposable schema")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
