"""Database metadata and reconciliation compatibility for bank workflows."""
import os
import sqlite3
import unittest
import uuid

from banking import (
    _columns,
    _table_exists,
    refresh_bank_transaction_reconciled,
    save_statement_line_match,
)


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

    def test_bank_statement_match_updates_reconciled_state_exactly_once(self):
        from postgres_database import connect_postgres

        connection = connect_postgres(POSTGRES_DSN)
        try:
            connection.execute("""
                CREATE TEMP TABLE bank_transactions (
                    id BIGINT PRIMARY KEY,
                    bank_account_id BIGINT NOT NULL,
                    destination_bank_account_id BIGINT,
                    journal_entry_id BIGINT NOT NULL UNIQUE,
                    reconciled INTEGER NOT NULL DEFAULT 0
                )
            """)
            connection.execute("""
                CREATE TEMP TABLE journal_lines (
                    id BIGINT PRIMARY KEY,
                    entry_id BIGINT NOT NULL,
                    account_id BIGINT NOT NULL
                )
            """)
            connection.execute("""
                CREATE TEMP TABLE bank_statement_lines (
                    id BIGINT PRIMARY KEY,
                    matched_journal_line_id BIGINT,
                    matched_transaction_id BIGINT,
                    matched_at TEXT,
                    matched_by BIGINT
                )
            """)
            connection.execute(
                "INSERT INTO bank_transactions(id,bank_account_id,journal_entry_id) VALUES(1,101,501)"
            )
            connection.execute(
                "INSERT INTO journal_lines(id,entry_id,account_id) VALUES(701,501,101)"
            )
            connection.execute("INSERT INTO bank_statement_lines(id) VALUES(801)")

            self.assertTrue(refresh_bank_transaction_reconciled(connection, 501))
            state = connection.execute(
                "SELECT reconciled FROM bank_transactions WHERE id=1"
            ).fetchone()["reconciled"]
            self.assertEqual(state, 0, "unmatched bank-side journal line must remain unreconciled")

            save_statement_line_match(
                connection, 801, 701, "2026-11-18T20:00:00", 9
            )
            self.assertTrue(refresh_bank_transaction_reconciled(connection, 501))
            state = connection.execute(
                "SELECT reconciled FROM bank_transactions WHERE id=1"
            ).fetchone()["reconciled"]
            self.assertEqual(state, 1, "all matched bank-side lines should reconcile the transaction")

            with self.assertRaisesRegex(ValueError, "تمت مطابقته بالفعل"):
                save_statement_line_match(
                    connection, 801, 701, "2026-11-18T20:01:00", 9
                )
        finally:
            connection.rollback()
            connection.close()


if __name__ == "__main__":
    unittest.main()
