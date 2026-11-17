"""Tests for the narrow cross-database schema inspection helpers."""
import os
import sqlite3
import unittest
import uuid
from types import SimpleNamespace

from db_sql import qmark_to_format

from db_compat import (
    database_dialect,
    table_columns,
    table_names,
    table_exists,
    begin_write_transaction,
    transaction_is_active,
    insert_and_get_id,
    execute_script,
    table_column_info,
    table_indexes,
)


class CrossDatabaseErrorTypeTests(unittest.TestCase):
    def test_sqlite_errors_remain_in_shared_database_error_types(self):
        from db_compat import DB_ERROR, DB_INTEGRITY_ERROR

        self.assertIn(sqlite3.Error, DB_ERROR)
        self.assertIn(sqlite3.IntegrityError, DB_INTEGRITY_ERROR)

    def test_integrity_errors_are_a_subset_of_database_errors(self):
        from db_compat import DB_ERROR, DB_INTEGRITY_ERROR

        for error_type in DB_INTEGRITY_ERROR:
            self.assertTrue(issubclass(error_type, DB_ERROR))


class SQLPlaceholderTranslationTests(unittest.TestCase):
    def test_translates_only_unquoted_qmark_placeholders(self):
        sql = '''SELECT ?, '?', "?", $tag$?;$tag$
            -- ?
            /* outer ? /* nested ? */ still ? */
            WHERE value=?'''
        expected = '''SELECT %s, '?', "?", $tag$?;$tag$
            -- ?
            /* outer ? /* nested ? */ still ? */
            WHERE value=%s'''
        self.assertEqual(qmark_to_format(sql), expected)

    def test_respects_postgresql_escape_string_literals(self):
        sql = "SELECT E'escaped \\' ?' AS label, ? AS value"
        expected = "SELECT E'escaped \\' ?' AS label, %s AS value"
        self.assertEqual(qmark_to_format(sql), expected)

    def test_dollar_inside_unquoted_identifier_is_not_a_dollar_quote(self):
        sql = "SELECT column$tag$ FROM probe WHERE id=?"
        self.assertEqual(
            qmark_to_format(sql),
            "SELECT column$tag$ FROM probe WHERE id=%s",
        )

    def test_rejects_unterminated_sql_constructs(self):
        cases = (
            "SELECT 'unfinished ?",
            'SELECT "unfinished ?',
            "SELECT /* unfinished ?",
            "SELECT $body$unfinished ?",
        )
        for sql in cases:
            with self.subTest(sql=sql):
                with self.assertRaises(ValueError):
                    qmark_to_format(sql)


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

    def test_schema_introspection_includes_generated_columns(self):
        self.connection.execute(
            "ALTER TABLE compatibility_probe ADD COLUMN normalized_amount "
            "TEXT GENERATED ALWAYS AS (lower(amount)) STORED"
        )
        self.assertIn("normalized_amount", table_columns(self.connection, "compatibility_probe"))
        info = table_column_info(self.connection, "compatibility_probe")
        self.assertIn("normalized_amount", info)
        self.assertEqual(info["normalized_amount"]["type"].upper(), "TEXT")
        self.assertEqual(info["normalized_amount"]["pk"], 0)
        self.assertTrue(info["normalized_amount"]["generated"])
        self.assertFalse(info["amount"]["generated"])

    def test_returns_portable_column_types_and_primary_key_position(self):
        info = table_column_info(self.connection, "compatibility_probe")
        self.assertEqual(info["id"]["pk"], 1)
        self.assertEqual(info["id"]["type"].upper(), "INTEGER")
        self.assertEqual(info["amount"]["type"].upper(), "TEXT")
        self.assertEqual(info["active"]["pk"], 0)
        self.assertFalse(info["id"]["generated"])
        self.assertFalse(info["amount"]["generated"])
        self.assertEqual(table_column_info(self.connection, "missing_table"), {})

    def test_lists_tables_in_current_schema(self):
        self.assertIn("compatibility_probe", table_names(self.connection))
        self.assertNotIn("missing_table", table_names(self.connection))

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


class PortableIndexInspectionTests(unittest.TestCase):
    def test_sqlite_returns_index_names(self):
        connection = sqlite3.connect(":memory:")
        try:
            connection.execute("CREATE TABLE index_probe (id INTEGER PRIMARY KEY, code TEXT)")
            connection.execute("CREATE INDEX idx_index_probe_code ON index_probe(code)")
            self.assertIn("idx_index_probe_code", table_indexes(connection))
        finally:
            connection.close()

    def test_postgresql_uses_current_schema_catalog(self):
        class Cursor:
            def fetchall(self):
                return [("idx_probe_code",), ("idx_probe_pk",)]

        class Connection(_FakePsycopgConnection):
            __module__ = "psycopg.fake"

            def execute(self, statement, params=()):
                self.statements.append((statement, tuple(params)))
                return Cursor()

        connection = Connection("IDLE")
        self.assertEqual(table_indexes(connection), {"idx_probe_code", "idx_probe_pk"})
        self.assertIn("pg_catalog.pg_indexes", connection.statements[0][0])
        self.assertIn("current_schema()", connection.statements[0][0])

    def test_postgresql_schema_is_bound_and_validated(self):
        class Cursor:
            def fetchall(self):
                return [("idx_probe_code",)]

        class Connection(_FakePsycopgConnection):
            __module__ = "psycopg.fake"

            def execute(self, statement, params=()):
                self.statements.append((statement, tuple(params)))
                return Cursor()

        connection = Connection("IDLE")
        self.assertEqual(table_indexes(connection, schema="tenant_1"), {"idx_probe_code"})
        self.assertEqual(connection.statements[0][1], ("tenant_1",))
        with self.assertRaises(ValueError):
            table_indexes(connection, schema="tenant_1; DROP SCHEMA public")


class PortableScriptExecutionTests(unittest.TestCase):
    def test_sqlite_script_uses_native_execution(self):
        connection = sqlite3.connect(":memory:")
        try:
            execute_script(connection, """
                CREATE TABLE script_probe (id INTEGER PRIMARY KEY, label TEXT);
                INSERT INTO script_probe(label) VALUES('a;b');
            """)
            self.assertEqual(
                connection.execute("SELECT label FROM script_probe").fetchone()[0],
                "a;b",
            )
        finally:
            connection.close()

    def test_postgresql_script_splits_only_statement_delimiters(self):
        connection = _FakePsycopgConnection("IDLE")
        execute_script(connection, """
            -- leading comment with ; delimiter
            CREATE TABLE script_probe (id BIGINT, label TEXT);
            INSERT INTO script_probe(label) VALUES('a;b');
            /* outer block ; /* nested block ; */ still a comment ; */
            INSERT INTO script_probe(label) VALUES(E'escaped \\' ; still string');
            DO $nexus$
            BEGIN
                PERFORM 1;
            END
            $nexus$;
        """)
        self.assertEqual(len(connection.statements), 4)
        self.assertIn("CREATE TABLE script_probe", connection.statements[0])
        self.assertIn("'a;b'", connection.statements[1])
        self.assertIn("E'escaped \\' ; still string'", connection.statements[2])
        self.assertIn("PERFORM 1;", connection.statements[3])

    def test_postgresql_script_converts_sqlite_integer_primary_key_to_identity(self):
        connection = _FakePsycopgConnection("IDLE")
        execute_script(connection, """
            CREATE TABLE sales_invoices (
                id INTEGER PRIMARY KEY,
                invoice_no TEXT NOT NULL,
                customer_id INTEGER
            );
        """)
        self.assertIn(
            "id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY",
            connection.statements[0],
        )
        self.assertIn("customer_id INTEGER", connection.statements[0])

    def test_postgresql_script_keeps_dollar_inside_identifier(self):
        connection = _FakePsycopgConnection("IDLE")
        execute_script(
            connection,
            "CREATE TABLE dollar_identifier_probe (column$tag$ TEXT); SELECT 1;",
        )
        self.assertEqual(len(connection.statements), 2)
        self.assertIn("column$tag$", connection.statements[0])
        self.assertEqual(connection.statements[1], "SELECT 1")

    def test_postgresql_script_rejects_unterminated_quotes(self):
        connection = _FakePsycopgConnection("IDLE")
        with self.assertRaises(ValueError):
            execute_script(connection, "INSERT INTO probe(label) VALUES('unfinished)")
        self.assertEqual(connection.statements, [])


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
            __module__ = "psycopg.fake"

            def execute(self, statement, params=()):
                self.statements.append((statement, tuple(params)))
                return Cursor()
        connection = Connection("IDLE")
        self.assertEqual(insert_and_get_id(connection, "INSERT INTO generated_probe(label) VALUES(?)", ("x",)), 42)
        self.assertEqual(connection.statements, [("INSERT INTO generated_probe(label) VALUES(%s) RETURNING id", ("x",))])

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
            info = table_column_info(connection, "compatibility_probe")
            self.assertEqual(info["id"]["pk"], 1)
            self.assertEqual(info["amount"]["type"], "numeric")
            self.assertEqual(info["amount"]["pk"], 0)
            self.assertFalse(table_exists(connection, "missing_table"))
        finally:
            connection.rollback()
            connection.close()


if __name__ == "__main__":
    unittest.main()
