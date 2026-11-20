import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.audit_sqlite_postgres_migration import build_report, inspect_sqlite
from scripts.generate_postgres_schema import pg_type


def _target_info(sqlite_info, *, type_overrides=None):
    type_overrides = type_overrides or {}
    tables = {}
    for table, details in sqlite_info["tables"].items():
        tables[table] = {
            "columns": list(details["columns"]),
            "column_types": {
                column: type_overrides.get((table, column), _pg_family(
                    pg_type({"name": column, "type": details["column_types"][column]}, table)
                ))
                for column in details["columns"]
            },
            "row_count": 0,
        }
    return {
        "tables": tables,
        "foreign_keys": [],
        "baseline_marker_present": True,
        "baseline_sha256": "a" * 64,
        "dashboard_version_table_present": True,
    }


def _pg_family(value):
    value = value.strip().lower()
    if value.startswith("numeric"):
        return "numeric"
    if value == "double precision":
        return value
    if value == "bigint":
        return value
    if value == "bytea":
        return value
    return "text"


class PostgreSQLMigrationPreflightTests(unittest.TestCase):
    def _source(self, script):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "source.sqlite"
        connection = sqlite3.connect(path)
        try:
            connection.executescript(script)
            connection.commit()
        finally:
            connection.close()
        return str(path)

    def test_preflight_blocks_stale_rebuild_tables(self):
        source_path = self._source("""
            CREATE TABLE accounts(id INTEGER PRIMARY KEY, code TEXT NOT NULL);
            CREATE TABLE payroll_runs_rebuilt(id INTEGER PRIMARY KEY);
        """)
        source = inspect_sqlite(source_path)
        report = build_report(
            source, _target_info(source),
            {"safe_for_numeric_migration": True},
        )
        self.assertEqual(report["stale_rebuild_tables"], ["payroll_runs_rebuilt"])
        self.assertFalse(report["preflight_passed"])

    def test_preflight_blocks_postgresql_type_drift(self):
        source_path = self._source("""
            CREATE TABLE accounts(id INTEGER PRIMARY KEY, code TEXT NOT NULL);
        """)
        source = inspect_sqlite(source_path)
        target = _target_info(source, type_overrides={("accounts", "id"): "text"})
        report = build_report(
            source, target, {"safe_for_numeric_migration": True}
        )
        self.assertEqual(
            report["type_differences"],
            [{
                "table": "accounts",
                "columns": [{
                    "column": "id",
                    "sqlite_declared_type": "INTEGER",
                    "expected_postgres_type": "bigint",
                    "actual_postgres_type": "text",
                }],
            }],
        )
        self.assertFalse(report["preflight_passed"])


if __name__ == "__main__":
    unittest.main()
