import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.inspect_sqlite_schema import inspect_database


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
                    INSERT INTO parent(code) VALUES ('P-1');
                    INSERT INTO child(parent_id, amount) VALUES (1, '10.25');
                    """
                )
                connection.commit()
            finally:
                connection.close()

            result = inspect_database(path, include_row_counts=True)
            self.assertEqual(result["summary"], {
                "table_count": 2, "view_count": 1, "index_count": 1
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


if __name__ == "__main__":
    unittest.main()
