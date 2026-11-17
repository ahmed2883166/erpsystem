"""Database metadata compatibility used by purchasing and sales helpers."""
import os
import sqlite3
import unittest
import uuid

from purchases import _columns as purchase_columns, _table_exists as purchase_table_exists
from sales import _columns as sales_columns, _table_exists as sales_table_exists


class PurchasingSalesMetadataSQLiteTests(unittest.TestCase):
    def test_both_modules_use_shared_sqlite_metadata_helpers(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("CREATE TABLE metadata_probe(id INTEGER PRIMARY KEY, reference TEXT)")
            for table_exists, columns in (
                (purchase_table_exists, purchase_columns),
                (sales_table_exists, sales_columns),
            ):
                self.assertTrue(table_exists(connection, "metadata_probe"))
                self.assertFalse(table_exists(connection, "missing_probe"))
                self.assertEqual(columns(connection, "metadata_probe"), {"id", "reference"})
        finally:
            connection.close()


POSTGRES_DSN = os.environ.get("NEXUS_TEST_POSTGRES_DSN", "").strip()


@unittest.skipUnless(POSTGRES_DSN, "NEXUS_TEST_POSTGRES_DSN is not configured")
class PurchasingSalesMetadataPostgreSQLTests(unittest.TestCase):
    def test_both_modules_read_metadata_in_a_rolled_back_postgresql_schema(self):
        from postgres_database import connect_postgres

        connection = connect_postgres(POSTGRES_DSN)
        schema = "nexus_sales_purchase_meta_" + uuid.uuid4().hex
        try:
            connection.execute(f'CREATE SCHEMA "{schema}"')
            connection.execute(f'SET LOCAL search_path TO "{schema}"')
            connection.execute(
                "CREATE TABLE metadata_probe(id BIGINT PRIMARY KEY, reference TEXT NOT NULL)"
            )
            for table_exists, columns in (
                (purchase_table_exists, purchase_columns),
                (sales_table_exists, sales_columns),
            ):
                self.assertTrue(table_exists(connection, "metadata_probe"))
                self.assertFalse(table_exists(connection, "missing_probe"))
                self.assertEqual(columns(connection, "metadata_probe"), {"id", "reference"})
        finally:
            connection.rollback()
            connection.close()


if __name__ == "__main__":
    unittest.main()
