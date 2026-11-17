"""Tests for the narrow cross-database schema inspection helpers."""
import os
import sqlite3
import unittest
import uuid

from db_compat import database_dialect, table_columns, table_exists


class SQLiteSchemaCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(
            "CREATE TABLE compatibility_probe "
            "(id INTEGER PRIMARY KEY, amount TEXT NOT NULL, active INTEGER)"
        )

    def tearDown(self):
        self.connection.close()

    def test_detects_sqlite_and_existing_table(self):
        self.assertEqual(database_dialect(self.connection), "sqlite")
        self.assertTrue(table_exists(self.connection, "compatibility_probe"))
        self.assertFalse(table_exists(self.connection, "missing_table"))

    def test_returns_columns_without_assuming_column_order(self):
        self.assertEqual(
            table_columns(self.connection, "compatibility_probe"),
            {"id", "amount", "active"},
        )
        self.assertEqual(table_columns(self.connection, "missing_table"), set())

    def test_rejects_unsafe_identifiers(self):
        with self.assertRaises(ValueError):
            table_exists(self.connection, 'probe"; DROP TABLE accounts; --')
        with self.assertRaises(ValueError):
            table_columns(self.connection, "probe WHERE 1=1")


POSTGRES_DSN = os.environ.get("NEXUS_TEST_POSTGRES_DSN", "").strip()


@unittest.skipUnless(POSTGRES_DSN, "NEXUS_TEST_POSTGRES_DSN is not configured")
class PostgreSQLSchemaCompatibilityTests(unittest.TestCase):
    def test_schema_helpers_use_current_search_path(self):
        from postgres_database import connect_postgres

        connection = connect_postgres(POSTGRES_DSN)
        schema = "nexus_compat_" + uuid.uuid4().hex
        try:
            connection.execute(f'CREATE SCHEMA "{schema}"')
            connection.execute(f'SET LOCAL search_path TO "{schema}"')
            connection.execute(
                "CREATE TABLE compatibility_probe "
                "(id BIGINT PRIMARY KEY, amount NUMERIC(24,6) NOT NULL)"
            )
            self.assertEqual(database_dialect(connection), "postgresql")
            self.assertTrue(table_exists(connection, "compatibility_probe"))
            self.assertEqual(
                table_columns(connection, "compatibility_probe"),
                {"id", "amount"},
            )
            self.assertFalse(table_exists(connection, "missing_table"))
        finally:
            connection.rollback()
            connection.close()


if __name__ == "__main__":
    unittest.main()
