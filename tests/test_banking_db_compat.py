"""Database metadata compatibility used by bank reconciliation helpers."""
import os
import sqlite3
import unittest
import uuid

from banking import _columns, _table_exists


class BankingMetadataSQLiteTests(unittest.TestCase):
    def test_bank_helpers_read_table_and_columns_from_sqlite(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        try:
            connection.execute(
                "CREATE TABLE bank_statement_lines(id INTEGER PRIMARY KEY, matched_journal_line_id INTEGER)"
            )
            self.assertTrue(_table_exists(connection, "bank_statement_lines"))
            self.assertFalse(_table_exists(connection, "missing_bank_table"))
            self.assertEqual(
                _columns(connection, "bank_statement_lines"),
                {"id", "matched_journal_line_id"},
            )
        finally:
            connection.close()


POSTGRES_DSN = os.environ.get("NEXUS_TEST_POSTGRES_DSN", "").strip()


@unittest.skipUnless(POSTGRES_DSN, "NEXUS_TEST_POSTGRES_DSN is not configured")
class BankingMetadataPostgreSQLTests(unittest.TestCase):
    def test_bank_helpers_read_metadata_in_rolled_back_schema(self):
        from postgres_database import connect_postgres

        connection = connect_postgres(POSTGRES_DSN)
        schema = "nexus_banking_meta_" + uuid.uuid4().hex
        try:
            connection.execute(f'CREATE SCHEMA "{schema}"')
            connection.execute(f'SET LOCAL search_path TO "{schema}"')
            connection.execute(
                "CREATE TABLE bank_statement_lines(id BIGINT PRIMARY KEY, matched_journal_line_id BIGINT)"
            )
            self.assertTrue(_table_exists(connection, "bank_statement_lines"))
            self.assertFalse(_table_exists(connection, "missing_bank_table"))
            self.assertEqual(
                _columns(connection, "bank_statement_lines"),
                {"id", "matched_journal_line_id"},
            )
        finally:
            connection.rollback()
            connection.close()


if __name__ == "__main__":
    unittest.main()
