"""Tests for the narrow cross-database schema inspection helpers."""
import os
import sqlite3
import unittest
import uuid
from types import SimpleNamespace

from db_compat import (
    database_dialect,
    table_columns,
    table_exists,
    begin_write_transaction,
    transaction_is_active,
)


class SQLiteSchemaCompatibilityTests(unittest.TestCase):
    def test_begin_write_transaction_preserves_existing_sqlite_transaction(self):
        connection = sqlite3.connect(":memory:")
        try:
            connection.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY)")
            self.assertFalse(transaction_is_active(connection))
            self.assertTrue(begin_write_transaction(connection))
            self.assertTrue(connection.in_transaction)
            self.assertTrue(transaction_is_active(connection))
            connection.execute("INSERT INTO probe DEFAULT VALUES")
            self.assertFalse(begin_write_transaction(connection))
            connection.commit()
            self.assertFalse(transaction_is_active(connection))
            self.assertFalse(connection.in_transaction)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM probe").fetchone()[0], 1)
        finally:
            connection.close()

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


class _FakePsycopgConnection:
    __module__ = "psycopg.fake"

    def __init__(self, status_name):
        self.info = SimpleNamespace(
            transaction_status=SimpleNamespace(name=status_name)
        )
        self.statements = []

    def execute(self, statement):
        self.statements.append(statement)
        if statement.strip().upper() == "BEGIN":
            self.info.transaction_status = SimpleNamespace(name="INTRANS")


class PortableTransactionStateTests(unittest.TestCase):
    def test_postgresql_idle_connection_starts_one_transaction(self):
        connection = _FakePsycopgConnection("IDLE")
        self.assertFalse(transaction_is_active(connection))
        self.assertTrue(begin_write_transaction(connection))
        self.assertEqual(connection.statements, ["BEGIN"])
        self.assertTrue(transaction_is_active(connection))

    def test_postgresql_existing_transaction_is_not_nested(self):
        connection = _FakePsycopgConnection("INTRANS")
        self.assertTrue(transaction_is_active(connection))
        self.assertFalse(begin_write_transaction(connection))
        self.assertEqual(connection.statements, [])


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
