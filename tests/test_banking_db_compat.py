"""Database metadata and reconciliation compatibility for bank workflows."""
import os
import sqlite3
import unittest
import uuid

from banking import (
    _columns,
    is_cash_or_bank_account,
    _table_exists,
    _bank_statement_reference_expression,
    refresh_bank_transaction_reconciled,
    save_statement_line_match,
)
from chart_templates import account_metadata_for


class BankingMetadataSQLiteTests(unittest.TestCase):
    def test_bank_name_in_expense_account_is_not_classified_as_cash(self):
        metadata = account_metadata_for(
            "expense", "7242", "عمولات ومصاريف بنكية"
        )
        self.assertNotEqual(metadata["account_subtype"], "cash")
        self.assertEqual(metadata["account_subtype"], "finance_cost")

    def test_cash_account_classification_requires_registry_or_asset_type(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        try:
            connection.execute(
                "CREATE TABLE cash_accounts(account_id INTEGER PRIMARY KEY)"
            )
            connection.execute(
                "INSERT INTO cash_accounts(account_id) VALUES(1)"
            )
            registered_bank = connection.execute(
                "SELECT 1 AS id, 'مصرف تجاري' AS name_ar, 'asset' AS account_type"
            ).fetchone()
            bank_fee_expense = connection.execute(
                "SELECT 2 AS id, 'عمولات ومصاريف بنكية' AS name_ar, 'expense' AS account_type"
            ).fetchone()
            legacy_bank_asset = connection.execute(
                "SELECT 3 AS id, 'بنك قديم' AS name_ar, 'asset' AS account_type"
            ).fetchone()
            ordinary_expense = connection.execute(
                "SELECT 4 AS id, 'مصروف صيانة' AS name_ar, 'expense' AS account_type"
            ).fetchone()

            self.assertTrue(is_cash_or_bank_account(connection, registered_bank))
            self.assertFalse(is_cash_or_bank_account(connection, bank_fee_expense))
            self.assertTrue(is_cash_or_bank_account(connection, legacy_bank_asset))
            self.assertFalse(is_cash_or_bank_account(connection, ordinary_expense))
        finally:
            connection.close()

    def test_statement_reference_expression_is_portable_on_sqlite(self):
        connection = sqlite3.connect(":memory:")
        try:
            self.assertEqual(
                _bank_statement_reference_expression(connection),
                "COALESCE(t.transaction_no, 'قيد #'||mje.id)",
            )
        finally:
            connection.close()

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
    def test_statement_reference_expression_casts_integer_id_to_text(self):
        from postgres_database import connect_postgres

        connection = connect_postgres(POSTGRES_DSN, schema="public")
        try:
            expression = _bank_statement_reference_expression(connection)
            row = connection.execute(
                "SELECT " + expression + " AS transaction_no "
                "FROM (SELECT NULL::TEXT AS transaction_no) t "
                "CROSS JOIN (SELECT 17::BIGINT AS id) mje"
            ).fetchone()
            self.assertEqual(row["transaction_no"], "قيد #17")
        finally:
            connection.rollback()
            connection.close()

    def test_bank_helpers_read_metadata_in_rolled_back_schema(self):
        from postgres_database import connect_postgres

        connection = connect_postgres(POSTGRES_DSN, schema="public")
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

        connection = connect_postgres(POSTGRES_DSN, schema="public")
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


    def test_postgresql_transfer_reconciliation_waits_for_both_bank_accounts(self):
        from postgres_database import connect_postgres

        connection = connect_postgres(POSTGRES_DSN, schema="public")
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
                "INSERT INTO bank_transactions"
                "(id,bank_account_id,destination_bank_account_id,journal_entry_id) "
                "VALUES(2,101,202,502)"
            )
            connection.execute(
                "INSERT INTO journal_lines(id,entry_id,account_id) "
                "VALUES(702,502,101),(703,502,202)"
            )
            connection.execute(
                "INSERT INTO bank_statement_lines(id) VALUES(802),(803)"
            )

            self.assertTrue(refresh_bank_transaction_reconciled(connection, 502))
            state = connection.execute(
                "SELECT reconciled FROM bank_transactions WHERE id=2"
            ).fetchone()["reconciled"]
            self.assertEqual(state, 0)

            save_statement_line_match(
                connection, 802, 702, "2026-11-19T10:00:00", 9
            )
            self.assertTrue(refresh_bank_transaction_reconciled(connection, 502))
            state = connection.execute(
                "SELECT reconciled FROM bank_transactions WHERE id=2"
            ).fetchone()["reconciled"]
            self.assertEqual(
                state, 0,
                "a transfer must remain unreconciled while the destination bank side is unmatched",
            )

            save_statement_line_match(
                connection, 803, 703, "2026-11-19T10:01:00", 9
            )
            self.assertTrue(refresh_bank_transaction_reconciled(connection, 502))
            state = connection.execute(
                "SELECT reconciled FROM bank_transactions WHERE id=2"
            ).fetchone()["reconciled"]
            self.assertEqual(state, 1)
        finally:
            connection.rollback()
            connection.close()


if __name__ == "__main__":
    unittest.main()
