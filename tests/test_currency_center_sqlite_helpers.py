"""SQLite regression coverage for cross-dialect FX transaction helpers."""
import sqlite3
import unittest

import currency_center
from db_compat import begin_write_transaction, transaction_is_active


class CurrencySQLiteTransactionHelperTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE fx_transactions (
                id INTEGER PRIMARY KEY,
                reference TEXT NOT NULL,
                party_type TEXT NOT NULL,
                currency_code TEXT NOT NULL,
                transaction_date TEXT NOT NULL,
                balance_foreign TEXT NOT NULL,
                balance_base TEXT NOT NULL,
                status TEXT NOT NULL
            );
            CREATE TABLE fx_settlements (
                id INTEGER PRIMARY KEY,
                transaction_id INTEGER NOT NULL,
                settlement_date TEXT NOT NULL
            );
            CREATE TABLE fx_revaluations (
                id INTEGER PRIMARY KEY,
                transaction_id INTEGER NOT NULL,
                revaluation_date TEXT NOT NULL
            );
            INSERT INTO fx_transactions VALUES
                (1, 'FX-SQLITE-1', 'receivable', 'USD', '2026-01-01',
                 '100.00', '5000.00', 'open');
            INSERT INTO fx_settlements VALUES
                (1, 1, '2026-02-01'), (2, 1, '2026-03-01');
            INSERT INTO fx_revaluations VALUES (1, 1, '2026-03-15');
        """)

    def tearDown(self):
        self.conn.close()

    def test_begin_write_is_not_nested_and_detects_active_transaction(self):
        self.assertFalse(transaction_is_active(self.conn))
        self.assertTrue(begin_write_transaction(self.conn))
        self.assertTrue(transaction_is_active(self.conn))
        self.assertFalse(begin_write_transaction(self.conn))
        self.conn.rollback()
        self.assertFalse(transaction_is_active(self.conn))

    def test_open_row_latest_dates_and_compare_and_set(self):
        row = currency_center._open_fx_transaction_for_update(self.conn, 1)
        self.assertEqual(row["reference"], "FX-SQLITE-1")
        self.assertEqual(
            currency_center._latest_fx_date(self.conn, "settlement", 1),
            "2026-03-01",
        )
        self.assertEqual(
            currency_center._latest_fx_date(self.conn, "revaluation", 1),
            "2026-03-15",
        )
        with self.assertRaisesRegex(ValueError, "نوع سجل العملة"):
            currency_center._latest_fx_date(self.conn, "unexpected", 1)

        currency_center.update_open_fx_balance(
            self.conn, 1, "100.00", "40.00", "2000.00"
        )
        updated = self.conn.execute(
            "SELECT balance_foreign,balance_base,status "
            "FROM fx_transactions WHERE id=1"
        ).fetchone()
        self.assertEqual(updated["balance_foreign"], "40.00")
        self.assertEqual(updated["balance_base"], "2000.00")
        self.assertEqual(updated["status"], "open")

        with self.assertRaisesRegex(ValueError, "تغير رصيد المعاملة"):
            currency_center.update_open_fx_balance(
                self.conn, 1, "100.00", "0", "0"
            )

    def test_full_settlement_stores_zero_and_closes_transaction(self):
        currency_center.update_open_fx_balance(
            self.conn, 1, "100.00", "0", "0"
        )
        row = self.conn.execute(
            "SELECT balance_foreign,balance_base,status "
            "FROM fx_transactions WHERE id=1"
        ).fetchone()
        self.assertEqual(row["balance_foreign"], "0")
        self.assertEqual(row["balance_base"], "0.00")
        self.assertEqual(row["status"], "settled")


if __name__ == "__main__":
    unittest.main()
