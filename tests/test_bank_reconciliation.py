import sqlite3
import tempfile
import unittest
from pathlib import Path
from decimal import Decimal
from unittest.mock import patch

import banking

from banking import signed_ledger_amount, opening_reconciliation_state, count_same_amount_candidates


class BankStatementAmountTests(unittest.TestCase):
    def test_statement_line_can_only_be_claimed_once(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute("""CREATE TABLE bank_statement_lines(
            id INTEGER PRIMARY KEY, matched_transaction_id INTEGER,
            matched_journal_line_id INTEGER, matched_at TEXT, matched_by INTEGER
        )""")
        connection.execute("INSERT INTO bank_statement_lines(id) VALUES(1)")
        banking.save_statement_line_match(connection, 1, 10, "2026-11-15T10:00:00", 1)
        with self.assertRaisesRegex(ValueError, "تمت مطابقته"):
            banking.save_statement_line_match(connection, 1, 11, "2026-11-15T10:01:00", 2)
        row = connection.execute(
            "SELECT matched_journal_line_id,matched_by FROM bank_statement_lines WHERE id=1"
        ).fetchone()
        self.assertEqual((row["matched_journal_line_id"], row["matched_by"]), (10, 1))
        connection.close()

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


    def test_bank_opening_balance_excludes_copied_later_year_opening_entry(self):
        conn=sqlite3.connect(":memory:")
        conn.row_factory=sqlite3.Row
        conn.executescript("""
            CREATE TABLE journal_entries(id INTEGER PRIMARY KEY,entry_date TEXT,status TEXT);
            CREATE TABLE journal_lines(id INTEGER PRIMARY KEY,entry_id INTEGER,account_id INTEGER,debit TEXT,credit TEXT);
            CREATE TABLE financial_years(id INTEGER PRIMARY KEY,year_name TEXT,start_date TEXT,end_date TEXT,status TEXT,opening_entry_id INTEGER);
            CREATE TABLE financial_year_journal_history(id INTEGER PRIMARY KEY,financial_year_id INTEGER,entry_kind TEXT,action TEXT,entry_id INTEGER,related_entry_id INTEGER);
            INSERT INTO journal_entries VALUES(1,'2026-12-31','posted'),(2,'2027-01-01','posted');
            INSERT INTO journal_lines VALUES(1,1,10,'100','0'),(2,2,10,'100','0');
            INSERT INTO financial_years VALUES(1,'2027','2027-01-01','2027-12-31','open',2);
            INSERT INTO financial_year_journal_history(financial_year_id,entry_kind,action,entry_id)
              VALUES(1,'opening','created',2);
        """)
        self.assertEqual(banking.bank_opening_ledger_balance(conn,10,"2027-02-01"),Decimal("100.00"))
        conn.close()


    def test_bank_transfer_is_reconciled_only_after_both_bank_sides_match(self):
        conn=sqlite3.connect(":memory:")
        conn.row_factory=sqlite3.Row
        conn.executescript("""
            CREATE TABLE bank_transactions(id INTEGER PRIMARY KEY,bank_account_id INTEGER,destination_bank_account_id INTEGER,journal_entry_id INTEGER,reconciled INTEGER);
            CREATE TABLE journal_lines(id INTEGER PRIMARY KEY,entry_id INTEGER,account_id INTEGER);
            CREATE TABLE bank_statement_lines(id INTEGER PRIMARY KEY,matched_journal_line_id INTEGER);
            INSERT INTO bank_transactions VALUES(1,10,20,50,0);
            INSERT INTO journal_lines VALUES(100,50,10),(200,50,20);
            INSERT INTO bank_statement_lines VALUES(1,100);
        """)
        self.assertTrue(banking.refresh_bank_transaction_reconciled(conn,50))
        self.assertEqual(conn.execute("SELECT reconciled FROM bank_transactions WHERE id=1").fetchone()[0],0)
        conn.execute("INSERT INTO bank_statement_lines VALUES(2,200)")
        self.assertTrue(banking.refresh_bank_transaction_reconciled(conn,50))
        self.assertEqual(conn.execute("SELECT reconciled FROM bank_transactions WHERE id=1").fetchone()[0],1)
        conn.close()




class BankStatementCsvImportTests(unittest.TestCase):
    def test_imports_utf8_bom_csv_with_english_headers(self):
        rows = banking.parse_statement_csv(
            b"\xef\xbb\xbfDate,Amount,Reference,Description\n2026-11-01,1250.50,TX-1,Customer deposit\n2026-11-02,-25.00,TX-2,Bank fee\n"
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["statement_date"], "2026-11-01")
        self.assertEqual(rows[0]["signed_amount"], Decimal("1250.50"))
        self.assertEqual(rows[1]["signed_amount"], Decimal("-25.00"))
        self.assertEqual(rows[0]["bank_reference"], "TX-1")

    def test_imports_semicolon_csv_with_arabic_headers_and_local_date(self):
        rows = banking.parse_statement_csv(
            "التاريخ;المبلغ;المرجع;البيان\n01/11/2026;1,200.75;مرجع-١;إيداع نقدي\n"
        )
        self.assertEqual(rows[0]["statement_date"], "2026-11-01")
        self.assertEqual(rows[0]["signed_amount"], Decimal("1200.75"))
        self.assertEqual(rows[0]["description"], "إيداع نقدي")

    def test_accepts_decimal_comma_without_thousands_separator(self):
        rows = banking.parse_statement_csv("Date;Amount\n2026-11-01;1250,75\n")
        self.assertEqual(rows[0]["signed_amount"], Decimal("1250.75"))

    def test_rejects_ambiguous_comma_amount(self):
        with self.assertRaisesRegex(ValueError, "تنسيق مبلغ ملتبس"):
            banking.parse_statement_csv("Date;Amount\n2026-11-01;1,20,30\n")

    def test_rejects_csv_without_required_columns(self):
        with self.assertRaisesRegex(ValueError, "عمود للتاريخ وعمود للمبلغ"):
            banking.parse_statement_csv("Reference,Description\n1,Deposit\n")

    def test_rejects_invalid_date_and_zero_amount(self):
        with self.assertRaisesRegex(ValueError, "تاريخ غير صحيح"):
            banking.parse_statement_csv("Date,Amount\nnot-a-date,10\n")
        with self.assertRaisesRegex(ValueError, "لا يمكن أن يكون صفرًا"):
            banking.parse_statement_csv("Date,Amount\n2026-11-01,0\n")

    def test_rejects_empty_and_oversized_row_sets(self):
        with self.assertRaisesRegex(ValueError, "فارغ"):
            banking.parse_statement_csv("")
        with self.assertRaisesRegex(ValueError, "الحد الأقصى"):
            banking.parse_statement_csv("Date,Amount\n2026-11-01,1\n2026-11-02,2\n", max_rows=1)

    def test_rejects_non_utf8_bytes(self):
        with self.assertRaisesRegex(ValueError, "UTF-8"):
            banking.parse_statement_csv(b"Date,Amount\n2026-11-01,\xff\n")


if __name__ == "__main__":
    unittest.main()
