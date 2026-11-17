import unittest
from decimal import Decimal

from scripts.copy_sqlite_to_postgres import copy_database, dependency_order, normalize_value, order_self_referencing_rows, quote_ident
from scripts.verify_sqlite_postgres_copy import (
    _compare_decimal_maps,
    _decimal,
    _missing_control_columns,
    _journal_entry_key,
    _sqlite_grouped_sum,
    _verification_passed,
    verify_copy,
)


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


class SQLitePostgresVerifierUnitTests(unittest.TestCase):
    def test_decimal_parser_keeps_exact_financial_precision(self):
        self.assertEqual(_decimal("1250.10"), Decimal("1250.10"))
        self.assertEqual(_decimal(None), Decimal("0"))
        with self.assertRaisesRegex(ValueError, "Non-numeric"):
            _decimal("not-a-number")

    def test_sqlite_grouped_sums_preserve_party_keys_and_decimal_values(self):
        import sqlite3
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute(
                "CREATE TABLE journal_lines (customer_id INTEGER, debit TEXT)"
            )
            conn.executemany(
                "INSERT INTO journal_lines(customer_id, debit) VALUES (?, ?)",
                [(10, "10.10"), (10, "0.20"), (20, "5.00"), (None, "1.25")],
            )
            totals = _sqlite_grouped_sum(
                conn, "journal_lines", "debit", ("customer_id",)
            )
            self.assertEqual(totals[(10,)], Decimal("10.30"))
            self.assertEqual(totals[(20,)], Decimal("5.00"))
            self.assertEqual(totals[(None,)], Decimal("1.25"))
        finally:
            conn.close()

    def test_control_comparison_reports_missing_and_mismatched_totals(self):
        issues = []
        _compare_decimal_maps(
            "journal.debit",
            {(1,): Decimal("100.00"), (2,): Decimal("25.00")},
            {(1,): Decimal("99.99"), (3,): Decimal("5.00")},
            issues,
        )
        self.assertEqual(len(issues), 3)
        self.assertEqual(
            {tuple(item["key"]) for item in issues},
            {(1,), (2,), (3,)},
        )

    def test_journal_entry_reference_detection_supports_legacy_names(self):
        self.assertEqual(_journal_entry_key({"debit", "entry_id"}), "entry_id")
        self.assertEqual(_journal_entry_key({"journal_entry_id", "credit"}), "journal_entry_id")
        self.assertIsNone(_journal_entry_key({"debit", "credit"}))

    def test_missing_control_columns_are_reported_for_both_databases(self):
        source_missing, target_missing = _missing_control_columns(
            {"debit", "entry_id", "customer_id"},
            {"debit", "entry_id"},
            {"debit", "customer_id"},
        )
        self.assertEqual(source_missing, ["customer_id"])
        self.assertEqual(target_missing, ["entry_id"])

    def test_missing_journal_table_is_not_a_successful_verification(self):
        skipped = [{"control": "journal_lines.required", "reason": "source_table_missing"}]
        self.assertFalse(
            _verification_passed(
                [], skipped,
                {"accounts": {"sqlite": 1, "postgres": 1}},
                ["accounts"],
            )
        )

    def test_verification_fails_closed_when_any_control_is_skipped(self):
        complete_rows = {"accounts": {"sqlite": 2, "postgres": 2}}
        self.assertTrue(_verification_passed([], [], complete_rows, ["accounts"]))
        self.assertFalse(
            _verification_passed(
                [], [{"control": "journal.debit", "reason": "required_columns_missing"}],
                complete_rows, ["accounts"],
            )
        )
        self.assertFalse(_verification_passed([{"control": "row_count"}], [], complete_rows, ["accounts"]))
        self.assertFalse(_verification_passed([], [], {}, ["accounts"]))

    def test_journal_control_grouping_detects_reassigned_entry_lines(self):
        import sqlite3
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute(
                "CREATE TABLE journal_lines (entry_id INTEGER, debit TEXT)"
            )
            conn.executemany(
                "INSERT INTO journal_lines(entry_id, debit) VALUES (?, ?)",
                [(100, "50.00"), (100, "25.00"), (200, "75.00")],
            )
            totals = _sqlite_grouped_sum(
                conn, "journal_lines", "debit", ("entry_id",)
            )
            self.assertEqual(totals[(100,)], Decimal("75.00"))
            self.assertEqual(totals[(200,)], Decimal("75.00"))
            # Grand total alone is 150.00; the per-entry distribution is distinct.
            reassigned = {(100,): Decimal("50.00"), (200,): Decimal("100.00")}
            issues = []
            _compare_decimal_maps("journal.debit_by_entry", totals, reassigned, issues)
            self.assertEqual({tuple(item["key"]) for item in issues}, {(100,), (200,)})
        finally:
            conn.close()


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
                CREATE TABLE journal_lines (
                    id INTEGER PRIMARY KEY,
                    entry_id INTEGER NOT NULL REFERENCES entries(id),
                    account_id INTEGER NOT NULL REFERENCES accounts(id),
                    debit TEXT NOT NULL,
                    credit TEXT NOT NULL,
                    customer_id INTEGER,
                    supplier_id INTEGER
                );
            """)
            # executescript commits its own transaction; defer constraints only
            # after it, for the fixture's intentionally unordered parent rows.
            conn.execute("BEGIN")
            conn.execute("PRAGMA defer_foreign_keys = ON")
            conn.executemany(
                "INSERT INTO accounts(id, parent_id, name) VALUES (?, ?, ?)",
                [(3, 2, "Child"), (1, None, "Root"), (2, 1, "Group")],
            )
            amount = -5 if invalid_amount else 12.5
            conn.execute(
                "INSERT INTO entries(id, account_id, amount) VALUES (7, 3, ?)",
                (amount,),
            )
            conn.execute(
                "INSERT INTO journal_lines(id, entry_id, account_id, debit, credit, customer_id, supplier_id) "
                "VALUES (8, 7, 3, '12.50', '0.00', 11, 21)"
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
            conn.execute(f'''
                CREATE TABLE "{self.schema}"."journal_lines" (
                    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                    entry_id BIGINT NOT NULL REFERENCES "{self.schema}"."entries"(id),
                    account_id BIGINT NOT NULL REFERENCES "{self.schema}"."accounts"(id),
                    debit NUMERIC NOT NULL,
                    credit NUMERIC NOT NULL,
                    customer_id BIGINT,
                    supplier_id BIGINT
                )
            ''')

    def test_real_postgres_copy_preserves_ids_relationships_decimals_and_sequences(self):
        import psycopg
        self._create_target()
        source_path = self._source()
        result = copy_database(
            source_path, self.dsn, self.schema, batch_size=2, apply=True
        )
        self.assertTrue(result["committed"])
        self.assertEqual(result["total_rows"], 5)
        verification = verify_copy(source_path, self.dsn, self.schema)
        self.assertTrue(verification["passed"], verification["issues"])
        self.assertEqual(verification["source_table_count"], 3)
        self.assertEqual(verification["source_row_count"], 5)
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
