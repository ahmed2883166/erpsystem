import sqlite3
import tempfile
import unittest
from pathlib import Path
from decimal import Decimal
from unittest.mock import patch

import banking

from banking import signed_ledger_amount, opening_reconciliation_state, count_same_amount_candidates


class BankStatementAmountTests(unittest.TestCase):
    def test_debit_is_positive_in_bank_statement_direction(self):
        self.assertEqual(signed_ledger_amount("125.50", "0"), Decimal("125.50"))

    def test_credit_is_negative_in_bank_statement_direction(self):
        self.assertEqual(signed_ledger_amount("0", "40.00"), Decimal("-40.00"))

    def test_signed_amount_uses_exact_decimal_arithmetic(self):
        self.assertEqual(signed_ledger_amount("100.10", "0.01"), Decimal("100.09"))

    def test_empty_values_are_treated_as_zero(self):
        self.assertEqual(signed_ledger_amount(None, None), Decimal("0.00"))

    def test_existing_statement_table_gets_journal_line_match_column(self):
        with tempfile.TemporaryDirectory() as folder:
            database_path=Path(folder)/"migration.sqlite3"
            connection=sqlite3.connect(database_path,isolation_level=None)
            connection.row_factory=sqlite3.Row
            connection.execute("""CREATE TABLE bank_statement_lines(
                id INTEGER PRIMARY KEY,bank_account_id INTEGER NOT NULL,
                statement_date TEXT NOT NULL,signed_amount TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',bank_reference TEXT NOT NULL DEFAULT '',
                matched_transaction_id INTEGER,matched_side TEXT
                  CHECK(matched_side IN ('source','destination')),
                matched_at TEXT,matched_by INTEGER,created_by INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
            connection.close()
            def open_database():
                conn=sqlite3.connect(database_path,isolation_level=None)
                conn.row_factory=sqlite3.Row
                return conn
            with patch.object(banking,"db",side_effect=open_database):
                banking.init_bank_schema()
            check=sqlite3.connect(database_path)
            columns={row[1] for row in check.execute("PRAGMA table_info(bank_statement_lines)")}
            balance_columns={row[1] for row in check.execute("PRAGMA table_info(bank_statement_balances)")}
            indexes={row[1] for row in check.execute("PRAGMA index_list(bank_statement_lines)")}
            check.close()
            self.assertIn("matched_journal_line_id",columns)
            self.assertIn("idx_bank_statement_journal_line",indexes)
            self.assertIn("opening_reviewed",balance_columns)
            self.assertIn("opening_review_note",balance_columns)




class BankOpeningReconciliationTests(unittest.TestCase):
    def test_missing_statement_balances_do_not_create_zero_balance_difference(self):
        state=opening_reconciliation_state(None,Decimal("125.00"),Decimal("30.00"))
        self.assertFalse(state["balances_entered"])
        self.assertIsNone(state["opening_balance"])
        self.assertIsNone(state["expected_closing_balance"])
        self.assertIsNone(state["closing_difference"])
        self.assertFalse(state["opening_ready"])

    def test_opening_difference_blocks_closing_difference_until_reviewed(self):
        balances={"opening_balance":"1000.00","closing_balance":"1200.00",
                  "opening_reviewed":0,"opening_review_note":""}
        state=opening_reconciliation_state(balances,Decimal("900.00"),Decimal("200.00"))
        self.assertEqual(state["opening_difference"],Decimal("100.00"))
        self.assertFalse(state["opening_ready"])
        self.assertIsNone(state["closing_difference"])

    def test_matching_opening_balance_allows_closing_calculation(self):
        balances={"opening_balance":"1000.00","closing_balance":"1200.00",
                  "opening_reviewed":0,"opening_review_note":""}
        state=opening_reconciliation_state(balances,Decimal("1000.00"),Decimal("150.00"))
        self.assertTrue(state["opening_ready"])
        self.assertEqual(state["expected_closing_balance"],Decimal("1150.00"))
        self.assertEqual(state["closing_difference"],Decimal("50.00"))

    def test_reviewed_opening_difference_is_visible_and_documented(self):
        balances={"opening_balance":"1000.00","closing_balance":"1200.00",
                  "opening_reviewed":1,"opening_review_note":"شيكات معلقة من الشهر السابق"}
        state=opening_reconciliation_state(balances,Decimal("900.00"),Decimal("200.00"))
        self.assertTrue(state["opening_ready"])
        self.assertEqual(state["closing_difference"],Decimal("0.00"))
        self.assertIn("شيكات",state["opening_review_note"])

    def test_ambiguous_candidates_count_by_signed_amount_not_debit_credit_shape(self):
        rows=[
            {"debit":"100.00","credit":"0"},
            {"debit":"150.00","credit":"50.00"},
            {"debit":"0","credit":"100.00"},
        ]
        self.assertEqual(count_same_amount_candidates(rows,Decimal("100.00")),2)
        self.assertEqual(count_same_amount_candidates(rows,Decimal("-100.00")),1)


if __name__ == "__main__":
    unittest.main()
