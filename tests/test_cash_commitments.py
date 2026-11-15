import sqlite3
import unittest
from decimal import Decimal

from cash_commitments import (
    initialize_cash_commitments_schema,
    create_cash_commitment,
    record_cash_commitment_payment,
    void_cash_commitment,
    list_cash_commitments_forecast,
)


class CashCommitmentsTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("""CREATE TABLE journal_entries(
            id INTEGER PRIMARY KEY, status TEXT NOT NULL
        )""")
        self.conn.executemany(
            "INSERT INTO journal_entries(id,status) VALUES(?,?)",
            [(1, "posted"), (2, "draft"), (3, "posted")],
        )
        initialize_cash_commitments_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def _commitment(self, amount="1000", due="2026-11-25"):
        return create_cash_commitment(
            self.conn, "tax", "ضريبة قيمة مضافة", amount, due,
            period_label="2026-10", currency_code="EGP",
        )

    def test_schema_initialization_is_idempotent(self):
        initialize_cash_commitments_schema(self.conn)
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='cash_commitments'"
        ).fetchone())

    def test_partial_payments_and_forecast_as_of_date(self):
        commitment_id = self._commitment()
        record_cash_commitment_payment(
            self.conn, commitment_id, "2026-11-10", "200", 1
        )
        record_cash_commitment_payment(
            self.conn, commitment_id, "2026-11-20", "100", 3
        )
        report = list_cash_commitments_forecast(
            self.conn, "2026-11-15", "2026-12-15"
        )
        self.assertEqual(len(report), 1)
        self.assertEqual(report[0]["balance"], Decimal("800.00"))
        status = self.conn.execute(
            "SELECT status FROM cash_commitments WHERE id=?", (commitment_id,)
        ).fetchone()["status"]
        self.assertEqual(status, "partially_paid")

    def test_payment_requires_existing_posted_journal_entry(self):
        commitment_id = self._commitment()
        with self.assertRaises(ValueError):
            record_cash_commitment_payment(
                self.conn, commitment_id, "2026-11-10", "100", 2
            )

    def test_cannot_overpay_or_reuse_journal_entry(self):
        commitment_id = self._commitment("100")
        record_cash_commitment_payment(
            self.conn, commitment_id, "2026-11-10", "60", 1
        )
        with self.assertRaises(ValueError):
            record_cash_commitment_payment(
                self.conn, commitment_id, "2026-11-11", "50", 3
            )
        with self.assertRaises(sqlite3.IntegrityError):
            record_cash_commitment_payment(
                self.conn, commitment_id, "2026-11-11", "10", 1
            )

    def test_paid_commitment_is_not_forecast_and_void_is_excluded(self):
        paid_id = self._commitment("50")
        record_cash_commitment_payment(
            self.conn, paid_id, "2026-11-10", "50", 1
        )
        void_id = self._commitment("70", "2026-11-26")
        void_cash_commitment(self.conn, void_id)
        self.assertEqual(
            list_cash_commitments_forecast(self.conn, "2026-11-15", "2026-12-15"),
            [],
        )

    def test_future_dated_full_payment_does_not_hide_commitment_as_of_earlier_date(self):
        commitment_id = self._commitment("100")
        record_cash_commitment_payment(
            self.conn, commitment_id, "2026-11-20", "100", 1
        )
        report = list_cash_commitments_forecast(
            self.conn, "2026-11-15", "2026-12-15"
        )
        self.assertEqual(len(report), 1)
        self.assertEqual(report[0]["balance"], Decimal("100.00"))
        report_after_payment = list_cash_commitments_forecast(
            self.conn, "2026-11-21", "2026-12-15"
        )
        self.assertEqual(report_after_payment, [])

    def test_duplicate_source_reference_is_rejected(self):
        create_cash_commitment(
            self.conn, "friday_overtime", "بدل جمعة", "150", "2026-11-30",
            origin_type="friday_allowance_batch", origin_id=22,
        )
        with self.assertRaises(sqlite3.IntegrityError):
            create_cash_commitment(
                self.conn, "friday_overtime", "بدل جمعة", "150", "2026-11-30",
                origin_type="friday_allowance_batch", origin_id=22,
            )


if __name__ == "__main__":
    unittest.main()
