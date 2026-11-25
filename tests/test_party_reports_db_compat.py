"""Party-report metadata access delegates to the shared database compatibility layer."""
import os
import sqlite3
import unittest

from party_reports import _columns, _table_exists


class PartyReportsMetadataCompatibilityTests(unittest.TestCase):
    def test_sqlite_table_and_columns(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("CREATE TABLE customers (id INTEGER PRIMARY KEY, code TEXT, name TEXT)")
            self.assertTrue(_table_exists(connection, "customers"))
            self.assertFalse(_table_exists(connection, "missing_table"))
            self.assertEqual(_columns(connection, "customers"), {"id", "code", "name"})
            self.assertEqual(_columns(connection, "missing_table"), set())
        finally:
            connection.close()

    @unittest.skipUnless(os.environ.get("NEXUS_TEST_POSTGRES_DSN"),
                         "NEXUS_TEST_POSTGRES_DSN is not configured")
    def test_postgresql_table_and_columns_in_rolled_back_schema(self):
        import psycopg
        from psycopg.rows import dict_row

        connection = psycopg.connect(os.environ["NEXUS_TEST_POSTGRES_DSN"],
                                     row_factory=dict_row)
        schema = "nexus_party_meta_test"
        try:
            connection.execute(f'CREATE SCHEMA "{schema}"')
            connection.execute(f'SET search_path TO "{schema}"')
            connection.execute("CREATE TABLE customers (id BIGINT PRIMARY KEY, code TEXT, name TEXT)")
            self.assertTrue(_table_exists(connection, "customers"))
            self.assertFalse(_table_exists(connection, "missing_table"))
            self.assertEqual(_columns(connection, "customers"), {"id", "code", "name"})
            self.assertEqual(_columns(connection, "missing_table"), set())
        finally:
            connection.rollback()
            connection.close()


if __name__ == "__main__":
    unittest.main()
