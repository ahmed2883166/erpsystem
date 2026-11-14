import sqlite3
import tempfile
import unittest
from pathlib import Path
from decimal import Decimal
from unittest.mock import patch

import banking

from banking import signed_ledger_amount


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
            indexes={row[1] for row in check.execute("PRAGMA index_list(bank_statement_lines)")}
            check.close()
            self.assertIn("matched_journal_line_id",columns)
            self.assertIn("idx_bank_statement_journal_line",indexes)


if __name__ == "__main__":
    unittest.main()
