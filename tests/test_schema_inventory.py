import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.inspect_sqlite_schema import inspect_database


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class SQLiteSchemaInventoryTests(unittest.TestCase):
    def test_exports_tables_columns_foreign_keys_indexes_and_optional_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "schema.db"
            connection = sqlite3.connect(path)
            try:
                connection.executescript(
                    """
                    PRAGMA foreign_keys = ON;
                    CREATE TABLE parent (
                        id INTEGER PRIMARY KEY,
                        code TEXT NOT NULL UNIQUE
                    );
                    CREATE TABLE child (
                        id INTEGER PRIMARY KEY,
                        parent_id INTEGER NOT NULL REFERENCES parent(id),
                        amount TEXT NOT NULL DEFAULT '0'
                    );
                    CREATE INDEX ix_child_parent ON child(parent_id);
                    CREATE VIEW child_view AS
                        SELECT id, parent_id FROM child;
                    CREATE TRIGGER child_no_delete
                    BEFORE DELETE ON child
                    BEGIN SELECT RAISE(ABORT, 'protected'); END;
                    INSERT INTO parent(code) VALUES ('P-1');
                    INSERT INTO child(parent_id, amount) VALUES (1, '10.25');
                    """
                )
                connection.commit()
            finally:
                connection.close()

            result = inspect_database(path, include_row_counts=True)
            self.assertEqual(result["summary"], {
                "table_count": 2, "view_count": 1, "index_count": 1,
                "trigger_count": 1, "column_count": 5, "foreign_key_count": 1,
            })
            child = next(item for item in result["tables"] if item["name"] == "child")
            self.assertEqual(child["row_count"], 1)
            self.assertEqual(child["columns"][2]["name"], "amount")
            self.assertEqual(child["foreign_keys"][0]["table"], "parent")
            self.assertTrue(any(
                index["name"] == "ix_child_parent"
                for index in child["indexes"]
            ))
            self.assertEqual(result["views"][0]["name"], "child_view")
            self.assertEqual(result["triggers"][0]["name"], "child_no_delete")
            self.assertEqual(result["foreign_key_violations"], [])
            self.assertEqual(result["integrity_check"], ["ok"])

    def test_refuses_database_outside_os_temporary_directory(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            path = Path(directory) / "outside-temp-root.db"
            connection = sqlite3.connect(path)
            connection.execute("CREATE TABLE sample(id INTEGER PRIMARY KEY)")
            connection.close()
            with self.assertRaisesRegex(ValueError, "outside the OS temporary directory"):
                inspect_database(path)

    def test_does_not_modify_inspected_database(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "readonly.db"
            connection = sqlite3.connect(path)
            connection.execute("CREATE TABLE sample(value TEXT)")
            connection.execute("INSERT INTO sample(value) VALUES ('unchanged')")
            connection.commit()
            connection.close()

            inspect_database(path)

            connection = sqlite3.connect(path)
            try:
                self.assertEqual(
                    connection.execute("SELECT value FROM sample").fetchone()[0],
                    "unchanged",
                )
            finally:
                connection.close()

    def test_full_application_schema_exports_from_fresh_temporary_database(self):
        completed = subprocess.run(
            [sys.executable, "scripts/export_initialized_sqlite_schema.py"],
            cwd=REPOSITORY_ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=240,
        )
        self.assertEqual(
            completed.returncode, 0,
            msg="Schema export failed.\nstdout:\n" + completed.stdout +
                "\nstderr:\n" + completed.stderr,
        )
        schema = json.loads(completed.stdout)
        table_names = {table["name"] for table in schema["tables"]}
        self.assertIn("journal_entries", table_names)
        self.assertIn("journal_lines", table_names)
        self.assertIn("sales_invoices", table_names)
        self.assertIn("supplier_bills", table_names)
        self.assertIn("stock_valuation_layers", table_names)
        self.assertIn("payroll_runs", table_names)
        self.assertGreater(schema["summary"]["table_count"], 80)
        self.assertGreater(schema["summary"]["foreign_key_count"], 20)
        self.assertEqual(schema["foreign_key_violations"], [])
        self.assertEqual(schema["integrity_check"], ["ok"])
        self.assertFalse(schema["source"]["business_data_included"])


if __name__ == "__main__":
    unittest.main()
