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
    insert_and_get_id,
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

class GeneratedIdCompatibilityTests(unittest.TestCase):
    def test_sqlite_insert_returns_id_without_committing(self):
        connection = sqlite3.connect(":memory:")
        try:
            connection.execute("CREATE TABLE generated_probe (id INTEGER PRIMARY KEY, label TEXT)")
            row_id = insert_and_get_id(connection, "INSERT INTO generated_probe(label) VALUES(?)", ("x",))
            self.assertEqual(row_id, 1)
            self.assertTrue(connection.in_transaction)
        finally:
            connection.close()

    def test_postgresql_insert_uses_returning_id(self):
        class Cursor:
            def fetchone(self):
                return {"id": 42}
        class Connection(_FakePsycopgConnection):
            def execute(self, statement, params=()):
                self.statements.append((statement, tuple(params)))
                return Cursor()
        connection = Connection("IDLE")
        self.assertEqual(insert_and_get_id(connection, "INSERT INTO generated_probe(label) VALUES(?)", ("x",)), 42)
        self.assertEqual(connection.statements, [("INSERT INTO generated_probe(label) VALUES(?) RETURNING id", ("x",))])

    def test_rejects_existing_returning_clause(self):
        connection = sqlite3.connect(":memory:")
        try:
            with self.assertRaises(ValueError):
                insert_and_get_id(connection, "INSERT INTO probe DEFAULT VALUES RETURNING id")
        finally:
            connection.close()


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
