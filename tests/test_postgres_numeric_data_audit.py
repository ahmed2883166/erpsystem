import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.audit_postgres_numeric_data import (
    _open_read_only_database,
    audit_numeric_candidates,
)


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
            connection.execute("CREATE TABLE sample(amount TEXT)")
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


if __name__ == "__main__":
    unittest.main()
