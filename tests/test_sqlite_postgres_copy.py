import unittest
from decimal import Decimal

from scripts.copy_sqlite_to_postgres import copy_database, dependency_order, normalize_value, order_self_referencing_rows, quote_ident


class SQLitePostgresCopyPlanningTests(unittest.TestCase):
    def test_dependency_order_places_parent_before_child(self):
        result = dependency_order(
            ["journal_lines", "accounts", "journal_entries"],
            {
                "journal_lines": {"journal_entries", "accounts"},
                "journal_entries": set(),
                "accounts": set(),
            },
        )
        self.assertLess(result.index("accounts"), result.index("journal_lines"))
        self.assertLess(result.index("journal_entries"), result.index("journal_lines"))

    def test_dependency_order_rejects_cycles(self):
        with self.assertRaisesRegex(ValueError, "Foreign-key cycle"):
            dependency_order(["a", "b"], {"a": {"b"}, "b": {"a"}})

    def test_float_values_are_normalized_without_binary_float(self):
        self.assertEqual(normalize_value(0.1), Decimal("0.1"))
        self.assertEqual(normalize_value("0.10"), "0.10")

    def test_self_referencing_rows_are_parent_first(self):
        rows = [
            {"id": 3, "parent_id": 2},
            {"id": 1, "parent_id": None},
            {"id": 2, "parent_id": 1},
        ]
        ordered = order_self_referencing_rows(
            rows,
            [{"id": 0, "seq": 0, "from": "parent_id", "to": "id"}],
            "accounts",
        )
        self.assertEqual([row["id"] for row in ordered], [1, 2, 3])

    def test_self_reference_cycles_are_rejected(self):
        rows = [{"id": 1, "parent_id": 2}, {"id": 2, "parent_id": 1}]
        with self.assertRaisesRegex(ValueError, "cycle"):
            order_self_referencing_rows(
                rows,
                [{"id": 0, "seq": 0, "from": "parent_id", "to": "id"}],
                "accounts",
            )

    def test_copy_requires_explicit_apply_before_any_connection(self):
        with self.assertRaisesRegex(ValueError, "disabled by default"):
            copy_database("missing.sqlite", "postgresql://invalid", apply=False)

    def test_copy_refuses_public_schema_before_connecting(self):
        with self.assertRaisesRegex(ValueError, "dedicated, isolated"):
            copy_database("missing.sqlite", "postgresql://invalid", schema="public", apply=True)

    def test_identifiers_are_quoted(self):
        self.assertEqual(quote_ident('odd"name'), '"odd""name"')


if __name__ == "__main__":
    unittest.main()


class SQLitePostgresCopyIntegrationTests(unittest.TestCase):
    """Real PostgreSQL coverage; enabled only by the isolated CI service DSN."""

    @classmethod
    def setUpClass(cls):
        import os
        cls.dsn = os.environ.get("NEXUS_TEST_POSTGRES_DSN")
        if not cls.dsn:
            raise unittest.SkipTest("NEXUS_TEST_POSTGRES_DSN is not configured")
        try:
            import psycopg  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("psycopg is not installed")

    def setUp(self):
        import psycopg
        import uuid
        self.schema = "copy_test_" + uuid.uuid4().hex[:12]
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            conn.execute(f'CREATE SCHEMA "{self.schema}"')

    def tearDown(self):
        import psycopg
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE')

    def _source(self, *, invalid_amount=False):
        import sqlite3
        import tempfile
        from pathlib import Path
        handle = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
        handle.close()
        path = Path(handle.name)
        conn = sqlite3.connect(path)
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.executescript("""
                CREATE TABLE accounts (
                    id INTEGER PRIMARY KEY,
                    parent_id INTEGER REFERENCES accounts(id),
                    name TEXT NOT NULL
                );
                CREATE TABLE entries (
                    id INTEGER PRIMARY KEY,
                    account_id INTEGER NOT NULL REFERENCES accounts(id),
                    amount NUMERIC NOT NULL
                );
            """)
            # executescript commits its own transaction; defer constraints only
            # after it, for the fixture's intentionally unordered parent rows.
            conn.execute("PRAGMA defer_foreign_keys = ON")
            conn.execute("BEGIN")
            conn.executemany(
                "INSERT INTO accounts(id, parent_id, name) VALUES (?, ?, ?)",
                [(3, 2, "Child"), (1, None, "Root"), (2, 1, "Group")],
            )
            amount = -5 if invalid_amount else 12.5
            conn.execute(
                "INSERT INTO entries(id, account_id, amount) VALUES (7, 3, ?)",
                (amount,),
            )
            conn.commit()
        finally:
            conn.close()
        self.addCleanup(path.unlink, missing_ok=True)
        return str(path)

    def _create_target(self, *, reject_negative=False):
        import psycopg
        check = " CHECK (amount >= 0)" if reject_negative else ""
        with psycopg.connect(self.dsn) as conn:
            conn.execute(f'''
                CREATE TABLE "{self.schema}"."accounts" (
                    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                    parent_id BIGINT REFERENCES "{self.schema}"."accounts"(id),
                    name TEXT NOT NULL
                )
            ''')
            conn.execute(f'''
                CREATE TABLE "{self.schema}"."entries" (
                    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                    account_id BIGINT NOT NULL REFERENCES "{self.schema}"."accounts"(id),
                    amount NUMERIC NOT NULL{check}
                )
            ''')

    def test_real_postgres_copy_preserves_ids_relationships_decimals_and_sequences(self):
        import psycopg
        self._create_target()
        result = copy_database(
            self._source(), self.dsn, self.schema, batch_size=2, apply=True
        )
        self.assertTrue(result["committed"])
        self.assertEqual(result["total_rows"], 4)
        with psycopg.connect(self.dsn) as conn:
            accounts = conn.execute(
                f'SELECT id, parent_id FROM "{self.schema}"."accounts" ORDER BY id'
            ).fetchall()
            self.assertEqual(accounts, [(1, None), (2, 1), (3, 2)])
            amount = conn.execute(
                f'SELECT amount FROM "{self.schema}"."entries" WHERE id = 7'
            ).fetchone()[0]
            self.assertEqual(amount, Decimal("12.5"))
            next_id = conn.execute(
                f'INSERT INTO "{self.schema}"."accounts"(parent_id, name) '
                "VALUES (1, 'Sequence check') RETURNING id"
            ).fetchone()[0]
            self.assertEqual(next_id, 4)

    def test_real_postgres_copy_rolls_back_all_tables_on_insert_failure(self):
        import psycopg
        self._create_target(reject_negative=True)
        with self.assertRaises(Exception):
            copy_database(
                self._source(invalid_amount=True), self.dsn, self.schema,
                batch_size=1, apply=True,
            )
        with psycopg.connect(self.dsn) as conn:
            account_count = conn.execute(
                f'SELECT COUNT(*) FROM "{self.schema}"."accounts"'
            ).fetchone()[0]
            entry_count = conn.execute(
                f'SELECT COUNT(*) FROM "{self.schema}"."entries"'
            ).fetchone()[0]
        self.assertEqual(account_count, 0)
        self.assertEqual(entry_count, 0)
