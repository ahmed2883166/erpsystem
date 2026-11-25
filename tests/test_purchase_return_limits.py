import sqlite3
import unittest
from decimal import Decimal

from purchases import _supplier_bill_returnable_quantity


class SupplierReturnLimitTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE goods_receipts(
                id INTEGER PRIMARY KEY, warehouse_id INTEGER NOT NULL
            );
            CREATE TABLE goods_receipt_lines(
                id INTEGER PRIMARY KEY, receipt_id INTEGER NOT NULL
            );
            CREATE TABLE supplier_bill_inventory_lines(
                id INTEGER PRIMARY KEY, bill_id INTEGER NOT NULL,
                receipt_line_id INTEGER NOT NULL, product_id INTEGER NOT NULL,
                billed_quantity TEXT NOT NULL
            );
            CREATE TABLE supplier_credit_notes(
                id INTEGER PRIMARY KEY, bill_id INTEGER NOT NULL,
                status TEXT NOT NULL
            );
            CREATE TABLE supplier_credit_note_lines(
                id INTEGER PRIMARY KEY, credit_note_id INTEGER NOT NULL,
                product_id INTEGER NOT NULL, warehouse_id INTEGER NOT NULL,
                quantity TEXT NOT NULL
            );
        """)
        self.conn.executemany(
            "INSERT INTO goods_receipts(id,warehouse_id) VALUES(?,?)",
            [(1, 10), (2, 10), (3, 20)],
        )
        self.conn.executemany(
            "INSERT INTO goods_receipt_lines(id,receipt_id) VALUES(?,?)",
            [(101, 1), (102, 2), (103, 3)],
        )
        self.conn.executemany(
            """INSERT INTO supplier_bill_inventory_lines
               (bill_id,receipt_line_id,product_id,billed_quantity)
               VALUES(?,?,?,?)""",
            [(5, 101, 7, "4"), (5, 102, 7, "3"), (5, 103, 7, "9")],
        )

    def tearDown(self):
        self.conn.close()

    def test_returnable_quantity_is_limited_to_billed_product_and_warehouse(self):
        available = _supplier_bill_returnable_quantity(self.conn, 5, 7, 10)
        self.assertEqual(available, Decimal("7"))

    def test_prior_posted_returns_reduce_remaining_quantity(self):
        self.conn.execute(
            "INSERT INTO supplier_credit_notes(id,bill_id,status) VALUES(1,5,'posted')"
        )
        self.conn.execute(
            """INSERT INTO supplier_credit_note_lines
               (credit_note_id,product_id,warehouse_id,quantity)
               VALUES(1,7,10,'2.5')"""
        )
        available = _supplier_bill_returnable_quantity(self.conn, 5, 7, 10)
        self.assertEqual(available, Decimal("4.5"))

    def test_cancelled_notes_do_not_reduce_returnable_quantity(self):
        self.conn.execute(
            "INSERT INTO supplier_credit_notes(id,bill_id,status) VALUES(1,5,'cancelled')"
        )
        self.conn.execute(
            """INSERT INTO supplier_credit_note_lines
               (credit_note_id,product_id,warehouse_id,quantity)
               VALUES(1,7,10,'2')"""
        )
        available = _supplier_bill_returnable_quantity(self.conn, 5, 7, 10)
        self.assertEqual(available, Decimal("7"))

    def test_missing_bill_line_details_safely_allow_no_return(self):
        available = _supplier_bill_returnable_quantity(self.conn, 999, 7, 10)
        self.assertEqual(available, Decimal("0"))


if __name__ == "__main__":
    unittest.main()
