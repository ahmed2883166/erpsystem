import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.audit_postgres_numeric_data import (
    _open_read_only_database,
    audit_numeric_candidates,
)
from scripts.audit_sqlite_postgres_migration import inspect_numeric_source


def _snapshot(columns):
    tables = [{
        "name": "sample",
        "columns": [
            {"name": name, "type": declared_type, "pk": 0}
            for name, declared_type in columns
        ],
        "foreign_keys": [],
        "indexes": [],
    }]
    # The production generator intentionally requires its reviewed core overrides.
    for table_name, column_name in (
        ("journal_lines", "debit"),
        ("journal_lines", "credit"),
        ("currencies", "rate_to_base"),
        ("tax_rates", "rate"),
        ("fx_exchange_rates", "rate_to_base"),
    ):
        existing = next((item for item in tables if item["name"] == table_name), None)
        if existing is None:
            existing = {"name": table_name, "columns": [], "foreign_keys": [], "indexes": []}
            tables.append(existing)
        existing["columns"].append({"name": column_name, "type": "TEXT", "pk": 0})
    return {"tables": tables, "views": [], "triggers": [], "indexes": []}


class PostgreSQLNumericDataAuditTests(unittest.TestCase):
    def test_preserves_exact_decimal_strings_and_reports_precision_without_float(self):
        connection = sqlite3.connect(":memory:")
        try:
            connection.executescript("""
                CREATE TABLE sample(amount TEXT);
                CREATE TABLE journal_lines(debit TEXT, credit TEXT);
                CREATE TABLE currencies(rate_to_base TEXT);
                CREATE TABLE tax_rates(rate TEXT);
                CREATE TABLE fx_exchange_rates(rate_to_base TEXT);
                INSERT INTO journal_lines VALUES('0.00','0.00');
                INSERT INTO currencies VALUES('1.0000000000');
                INSERT INTO tax_rates VALUES('0.140000');
                INSERT INTO fx_exchange_rates VALUES('1.0000000000');
            """)
            connection.executemany(
                "INSERT INTO sample(amount) VALUES(?)",
                [("0.10",), ("12345678901234567890.123456789",), ("-2.50",), (None,)],
            )
            report = audit_numeric_candidates(connection, _snapshot([("amount", "TEXT")]))
            self.assertTrue(report["safe_for_numeric_migration"])
            self.assertEqual(report["candidate_count"], 1)
            item = report["columns"][0]
            self.assertEqual(item["status"], "convertible_exact_decimal")
            self.assertEqual(item["min_value"], "-2.50")
            self.assertEqual(item["max_value"], "12345678901234567890.123456789")
            self.assertEqual(item["max_integer_digits"], 20)
            self.assertEqual(item["max_fractional_digits"], 9)
        finally:
            connection.close()

    def test_empty_invalid_and_unpopulated_candidates_block_approval(self):
        connection = sqlite3.connect(":memory:")
        try:
            connection.executescript("""
                CREATE TABLE sample(
                    amount TEXT, blank_amount TEXT, invalid_cost TEXT,
                    empty_qty TEXT, no_rows_total TEXT
                );
                INSERT INTO sample(amount, blank_amount, invalid_cost, empty_qty)
                    VALUES('12.30', '', 'not-money', NULL);
                INSERT INTO sample(amount, blank_amount, invalid_cost, empty_qty)
                    VALUES('0.01', '   ', 'NaN', NULL);
                INSERT INTO sample(amount, blank_amount, invalid_cost, empty_qty)
                    VALUES('1.00', NULL, '1_000', NULL);
            """)
            report = audit_numeric_candidates(connection, _snapshot([
                ("amount", "TEXT"), ("blank_amount", "TEXT"),
                ("invalid_cost", "TEXT"), ("empty_qty", "TEXT"),
                ("no_rows_total", "TEXT"),
            ]))
            self.assertFalse(report["safe_for_numeric_migration"])
            self.assertEqual(report["candidate_count"], 5)
            self.assertEqual(report["status_counts"], {
                "convertible_exact_decimal": 1,
                "empty_values_require_policy": 1,
                "invalid_values": 1,
                "no_source_data": 2,
            })
            invalid = next(item for item in report["columns"] if item["column"] == "invalid_cost")
            self.assertEqual(invalid["invalid_count"], 3)
            self.assertEqual(invalid["invalid_sample_positions"], [1, 2, 3])
        finally:
            connection.close()

    def test_empty_numeric_candidate_is_safe_and_partial_schema_is_audited(self):
        connection = sqlite3.connect(":memory:")
        try:
            connection.execute("CREATE TABLE sample(amount TEXT)")
            report = audit_numeric_candidates(connection, {
                "tables": [{
                    "name": "sample",
                    "columns": [{"name": "amount", "type": "TEXT", "pk": 0}],
                    "foreign_keys": [],
                    "indexes": [],
                }],
                "views": [],
                "triggers": [],
                "indexes": [],
            })
            self.assertTrue(report["safe_for_numeric_migration"])
            self.assertEqual(report["status_counts"], {"no_source_data": 1})
            self.assertEqual(report["override_audited_column_count"], 0)
        finally:
            connection.close()

    def test_postgres_numeric_digit_limit_is_enforced(self):
        connection = sqlite3.connect(":memory:")
        try:
            connection.execute("CREATE TABLE sample(amount TEXT)")
            connection.execute("INSERT INTO sample(amount) VALUES('1e131072')")
            report = audit_numeric_candidates(connection, _snapshot([("amount", "TEXT")]))
            self.assertFalse(report["safe_for_numeric_migration"])
            self.assertEqual(report["columns"][0]["status"], "postgres_numeric_limit_exceeded")
            self.assertEqual(report["columns"][0]["max_integer_digits"], 131073)
        finally:
            connection.close()

    def test_foreign_key_violations_block_numeric_migration(self):
        connection = sqlite3.connect(":memory:")
        try:
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute("CREATE TABLE parent(id INTEGER PRIMARY KEY)")
            connection.execute("CREATE TABLE child(parent_id INTEGER REFERENCES parent(id), amount TEXT)")
            connection.execute("INSERT INTO child(parent_id, amount) VALUES(999, '1.25')")
            report = audit_numeric_candidates(connection, _snapshot([("amount", "TEXT")]))
            self.assertFalse(report["safe_for_numeric_migration"])
            self.assertEqual(report["foreign_key_violation_count"], 1)
            self.assertEqual(report["foreign_key_violations"][0]["table"], "child")
        finally:
            connection.close()

    def test_schema_type_drift_is_reported_not_coerced(self):
        connection = sqlite3.connect(":memory:")
        try:
            connection.execute("CREATE TABLE sample(amount REAL)")
            report = audit_numeric_candidates(connection, _snapshot([("amount", "TEXT")]))
            self.assertFalse(report["safe_for_numeric_migration"])
            self.assertEqual(report["columns"][0]["status"], "schema_mismatch")
            self.assertEqual(report["columns"][0]["actual_type"], "REAL")
        finally:
            connection.close()

    def test_integer_affinity_columns_reject_text_and_real_storage_classes(self):
        connection = sqlite3.connect(":memory:")
        try:
            connection.executescript("""
                CREATE TABLE sample(quantity INTEGER, optional_id BIGINT);
                INSERT INTO sample(quantity, optional_id) VALUES(12, 3);
                INSERT INTO sample(quantity, optional_id) VALUES('abc', 4);
                INSERT INTO sample(quantity, optional_id) VALUES(1.5, NULL);
                CREATE TABLE journal_lines(debit TEXT, credit TEXT);
                CREATE TABLE currencies(rate_to_base TEXT);
                CREATE TABLE tax_rates(rate TEXT);
                CREATE TABLE fx_exchange_rates(rate_to_base TEXT);
            """)
            report = audit_numeric_candidates(
                connection, _snapshot([
                    ("quantity", "INTEGER"), ("optional_id", "BIGINT")
                ])
            )
            self.assertFalse(report["safe_for_numeric_migration"])
            columns = {
                (item["table"], item["column"]): item
                for item in report["integer_storage_class_audit"]
            }
            self.assertEqual(
                columns[("sample", "quantity")]["status"],
                "non_integer_storage_class",
            )
            self.assertEqual(
                columns[("sample", "quantity")]["storage_class_counts"],
                {"integer": 1, "real": 1, "text": 1},
            )
            self.assertEqual(
                columns[("sample", "optional_id")]["status"],
                "integer_storage_only",
            )
        finally:
            connection.close()

    def test_integer_storage_audit_handles_integer_primary_key_rowid_alias(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "integer-primary-key.sqlite"
            connection = sqlite3.connect(path)
            try:
                connection.executescript("""
                    CREATE TABLE sample(id INTEGER PRIMARY KEY, quantity INTEGER);
                    INSERT INTO sample(quantity) VALUES('not-an-integer');
                """)
                connection.commit()
            finally:
                connection.close()

            report = inspect_numeric_source(str(path))
            self.assertFalse(report["safe_for_numeric_migration"])
            sample = next(
                item for item in report["integer_storage_class_audit"]
                if item["table"] == "sample" and item["column"] == "quantity"
            )
            self.assertEqual(sample["status"], "non_integer_storage_class")
            self.assertEqual(sample["invalid_samples"][0]["rowid"], 1)
            self.assertEqual(sample["invalid_samples"][0]["storage_class"], "text")
            self.assertEqual(sample["invalid_samples"][0]["value"], "not-an-integer")

    def test_database_connection_is_read_only(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "source.sqlite"
            connection = sqlite3.connect(path)
            connection.execute("CREATE TABLE sample(amount TEXT)")
            connection.commit()
            connection.close()
            read_only, _ = _open_read_only_database(path)
            try:
                with self.assertRaises(sqlite3.OperationalError):
                    read_only.execute("INSERT INTO sample(amount) VALUES('1.00')")
            finally:
                read_only.close()
            check = sqlite3.connect(path)
            try:
                self.assertEqual(check.execute("SELECT COUNT(*) FROM sample").fetchone()[0], 0)
            finally:
                check.close()

    def test_accepts_temporary_source_by_default(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "source.sqlite"
            connection = sqlite3.connect(path)
            connection.execute("CREATE TABLE sample(amount TEXT)")
            connection.close()
            # A temporary directory is accepted by default; non-temporary sources
            # require the explicit read-only override.
            read_only, _ = _open_read_only_database(path)
            read_only.close()


    def test_explicit_numeric_overrides_are_checked_for_scale_precision_and_real_sources(self):
        connection = sqlite3.connect(":memory:")
        try:
            connection.executescript("""
                CREATE TABLE sample(amount TEXT);
                INSERT INTO sample VALUES('1.00');
                CREATE TABLE journal_lines(debit TEXT, credit TEXT);
                INSERT INTO journal_lines VALUES('1.1234567','999999999999999999.99');
                CREATE TABLE currencies(rate_to_base REAL);
                INSERT INTO currencies VALUES(0.75);
                CREATE TABLE tax_rates(rate REAL);
                INSERT INTO tax_rates VALUES(14.0);
                CREATE TABLE fx_exchange_rates(rate_to_base TEXT);
                INSERT INTO fx_exchange_rates VALUES('1.12345678901');
            """)
            snapshot = _snapshot([("amount", "TEXT")])
            for table in snapshot["tables"]:
                if table["name"] in {"currencies", "tax_rates"}:
                    for column in table["columns"]:
                        if (table["name"], column["name"]) in {
                            ("currencies", "rate_to_base"), ("tax_rates", "rate")
                        }:
                            column["type"] = "REAL"
            report = audit_numeric_candidates(connection, snapshot)
            self.assertFalse(report["safe_for_numeric_migration"])
            overrides = {
                (item["table"], item["column"]): item
                for item in report["explicit_numeric_overrides"]
            }
            self.assertEqual(overrides[("journal_lines", "debit")]["status"], "precision_exceeds_target")
            self.assertEqual(overrides[("journal_lines", "credit")]["status"], "fits_declared_precision")
            self.assertEqual(overrides[("currencies", "rate_to_base")]["status"], "source_binary_float_requires_policy")
            self.assertEqual(overrides[("tax_rates", "rate")]["status"], "source_binary_float_requires_policy")
            self.assertEqual(overrides[("fx_exchange_rates", "rate_to_base")]["status"], "precision_exceeds_target")
            self.assertEqual(report["override_audited_column_count"], 5)
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
