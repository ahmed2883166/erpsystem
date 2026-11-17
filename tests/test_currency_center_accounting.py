"""Accounting safety baseline for the foreign-currency subledger."""
import sqlite3
import unittest
from decimal import Decimal

import currency_center


class CurrencyAmountTests(unittest.TestCase):
    def test_decimal_parser_rejects_invalid_and_non_finite_values(self):
        for value in ("not-a-number", "NaN", "Infinity", "-Infinity"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    currency_center.D(value)

    def test_money_rounds_half_up_to_two_decimal_places(self):
        self.assertEqual(currency_center.M("1.005"), Decimal("1.01"))
        self.assertEqual(currency_center.M("1.004"), Decimal("1.00"))
        self.assertEqual(currency_center.M("-1.005"), Decimal("-1.01"))


class CurrencyBalanceUpdateTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("""
            CREATE TABLE fx_transactions(
                id INTEGER PRIMARY KEY,
                balance_foreign TEXT NOT NULL,
                balance_base TEXT NOT NULL,
                status TEXT NOT NULL
            )
        """)
        self.conn.execute(
            "INSERT INTO fx_transactions(id,balance_foreign,balance_base,status) "
            "VALUES(1,'100.00','500.00','open')"
        )

    def tearDown(self):
        self.conn.close()

    def test_partial_settlement_keeps_transaction_open(self):
        currency_center.update_open_fx_balance(
            self.conn, 1, "100.00", Decimal("60.00"), Decimal("300.00")
        )
        row = self.conn.execute(
            "SELECT balance_foreign,balance_base,status FROM fx_transactions WHERE id=1"
        ).fetchone()
        self.assertEqual(row["balance_foreign"], "60.00")
        self.assertEqual(row["balance_base"], "300.00")
        self.assertEqual(row["status"], "open")

    def test_full_settlement_marks_transaction_settled(self):
        currency_center.update_open_fx_balance(
            self.conn, 1, "100.00", Decimal("0.00"), Decimal("0.00")
        )
        row = self.conn.execute(
            "SELECT balance_foreign,balance_base,status FROM fx_transactions WHERE id=1"
        ).fetchone()
        self.assertEqual(row["balance_foreign"], "0")
        self.assertEqual(row["balance_base"], "0.00")
        self.assertEqual(row["status"], "settled")

    def test_stale_expected_balance_is_rejected_without_overwriting(self):
        with self.assertRaisesRegex(ValueError, "تغير رصيد المعاملة"):
            currency_center.update_open_fx_balance(
                self.conn, 1, "90.00", Decimal("50.00"), Decimal("250.00")
            )
        row = self.conn.execute(
            "SELECT balance_foreign,balance_base,status FROM fx_transactions WHERE id=1"
        ).fetchone()
        self.assertEqual(row["balance_foreign"], "100.00")
        self.assertEqual(row["balance_base"], "500.00")
        self.assertEqual(row["status"], "open")


if __name__ == "__main__":
    unittest.main()
