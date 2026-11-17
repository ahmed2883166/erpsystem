"""Accounting safety baseline for the foreign-currency subledger."""
import os
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


@unittest.skipUnless(
    os.environ.get("NEXUS_TEST_POSTGRES_DSN"),
    "PostgreSQL integration DSN not configured",
)
class CurrencyBalanceUpdatePostgreSQLTests(unittest.TestCase):
    def test_full_settlement_updates_row_in_rolled_back_schema(self):
        import uuid
        import psycopg
        from psycopg.rows import dict_row

        schema = "nexus_fx_balance_" + uuid.uuid4().hex[:12]
        conn = psycopg.connect(
            os.environ["NEXUS_TEST_POSTGRES_DSN"], row_factory=dict_row
        )
        try:
            try:
                with conn.transaction():
                    conn.execute(f'CREATE SCHEMA "{schema}"')
                    conn.execute(f'SET LOCAL search_path TO "{schema}"')
                    conn.execute("""
                        CREATE TABLE fx_transactions (
                            id BIGINT PRIMARY KEY,
                            balance_foreign TEXT NOT NULL,
                            balance_base TEXT NOT NULL,
                            status TEXT NOT NULL
                        )
                    """)
                    conn.execute("""
                        INSERT INTO fx_transactions
                            (id,balance_foreign,balance_base,status)
                        VALUES (1,'100.00','500.00','open')
                    """)
                    currency_center.update_open_fx_balance(
                        conn, 1, "100.00", Decimal("0.00"), Decimal("0.00")
                    )
                    row = conn.execute("""
                        SELECT balance_foreign,balance_base,status
                        FROM fx_transactions WHERE id=1
                    """).fetchone()
                    self.assertEqual(
                        (row["balance_foreign"], row["balance_base"], row["status"]),
                        ("0", "0.00", "settled"),
                    )
                    raise RuntimeError("rollback disposable schema")
            except RuntimeError as exc:
                self.assertEqual(str(exc), "rollback disposable schema")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
