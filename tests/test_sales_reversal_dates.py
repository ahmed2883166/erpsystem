import sqlite3
import unittest
from decimal import Decimal
from unittest.mock import patch

from sales import _reverse_posted_journal


class SalesReversalDateTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("""
            CREATE TABLE journal_entries(
                id INTEGER PRIMARY KEY, status TEXT NOT NULL, entry_date TEXT NOT NULL
            )
        """)
        self.conn.execute(
            "INSERT INTO journal_entries(id,status,entry_date) VALUES(1,'posted','2026-05-12')"
        )
        self.conn.execute("""CREATE TABLE journal_lines(
            id INTEGER PRIMARY KEY, entry_id INTEGER, account_id INTEGER,
            debit TEXT, credit TEXT, memo TEXT,
            branch_id INTEGER, cost_center_id INTEGER, project_id INTEGER,
            customer_id INTEGER, salesperson_id INTEGER, supplier_id INTEGER,
            stock_product_id INTEGER, stock_warehouse_id INTEGER
        )""")
        self.conn.execute("""INSERT INTO journal_lines VALUES(
            1,1,10,'25','0','sale cost',2,3,4,5,6,7,81,91
        )""")

    def tearDown(self):
        self.conn.close()

    def test_customer_advance_header_lock_uses_for_update_on_postgresql(self):
        import sales

        class Row:
            def __getitem__(self, key):
                return 1

        with patch("sales.database_dialect", return_value="postgresql"), \
             patch("sales.execute_compat", return_value=type("Result", (), {"fetchone": lambda self: Row()})()) as execute:
            sales._lock_customer_advance_receipt(self.conn, 44)
        self.assertIn("FOR UPDATE", execute.call_args.args[1].upper())
        self.assertEqual(execute.call_args.args[2], (44,))

    def test_reversal_cannot_precede_original_journal_date(self):
        with self.assertRaisesRegex(ValueError, "يسبق تاريخ القيد الأصلي"):
            _reverse_posted_journal(
                self.conn, 1, "2026-05-11", "REV-1", "عكس اختبار", 1
            )

    def test_reversal_preserves_analytic_and_inventory_dimensions(self):
        with patch("sales.post_journal", return_value=2) as post:
            result = _reverse_posted_journal(
                self.conn, 1, "2026-05-13", "REV-2", "عكس بيع", 1
            )
        self.assertEqual(result, 2)
        reversed_lines = post.call_args.args[4]
        self.assertEqual(len(reversed_lines), 1)
        self.assertEqual(reversed_lines[0][0:3], (10, Decimal("0"), Decimal("25")))
        self.assertEqual(reversed_lines[0][4], {
            "branch_id": 2, "cost_center_id": 3, "project_id": 4,
            "customer_id": 5, "salesperson_id": 6, "supplier_id": 7,
            "stock_product_id": 81, "stock_warehouse_id": 91,
        })

    def test_invalid_reversal_date_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "تاريخ القيد العكسي غير صحيح"):
            _reverse_posted_journal(
                self.conn, 1, "not-a-date", "REV-1", "عكس اختبار", 1
            )


if __name__ == "__main__":
    unittest.main()
