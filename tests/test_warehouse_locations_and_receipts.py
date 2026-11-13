import sqlite3
import unittest
from decimal import Decimal
from unittest.mock import patch

from sales import post_customer_receipt_batch
from stock import consume_layers


class WarehouseLocationTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE stock_valuation_layers(
                id INTEGER PRIMARY KEY, product_id INTEGER, warehouse_id INTEGER,
                location_id INTEGER, lot_id INTEGER, qty_in TEXT, qty_remaining TEXT,
                unit_cost TEXT, value_remaining TEXT
            );
            INSERT INTO stock_valuation_layers
                (id,product_id,warehouse_id,location_id,qty_in,qty_remaining,unit_cost,value_remaining)
            VALUES (1,10,2,101,'10','10','10','100'),
                   (2,10,2,102,'5','5','12','60');
        """)

    def tearDown(self):
        self.conn.close()

    def test_issue_can_consume_only_from_selected_storage_location(self):
        total, allocations = consume_layers(self.conn, 10, 2, Decimal("4"), "fifo",
                                            location_id=101)
        self.assertEqual(total, Decimal("40.00"))
        self.assertEqual(self.conn.execute(
            "SELECT qty_remaining FROM stock_valuation_layers WHERE id=1"
        ).fetchone()["qty_remaining"], "6.0000")
        self.assertEqual(self.conn.execute(
            "SELECT qty_remaining FROM stock_valuation_layers WHERE id=2"
        ).fetchone()["qty_remaining"], "5")


class ConsolidatedCustomerReceiptTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE customers(id INTEGER PRIMARY KEY, name TEXT, active INTEGER);
            INSERT INTO customers VALUES(1,'عميل تجريبي',1);
            CREATE TABLE accounts(
                id INTEGER PRIMARY KEY, code TEXT NOT NULL, is_group INTEGER,
                active INTEGER, account_type TEXT
            );
            INSERT INTO accounts(id,code,is_group,active,account_type) VALUES
                (1,'1110',0,1,'asset'),
                (2,'1120',0,1,'asset');
            CREATE TABLE sales_invoices(
                id INTEGER PRIMARY KEY, invoice_no TEXT, customer_id INTEGER, status TEXT,
                total_amount TEXT, paid_amount TEXT, credited_amount TEXT
            );
            INSERT INTO sales_invoices VALUES
                (11,'INV-11',1,'posted','1000','200','0'),
                (12,'INV-12',1,'posted','500','0','0');
            CREATE TABLE customer_receipt_batches(
                id INTEGER PRIMARY KEY, receipt_no TEXT, customer_id INTEGER, receipt_date TEXT,
                total_amount TEXT, cash_account_id INTEGER, journal_entry_id INTEGER,
                reference TEXT, created_by INTEGER
            );
            CREATE TABLE customer_receipt_allocations(
                id INTEGER PRIMARY KEY,batch_id INTEGER,invoice_id INTEGER,amount TEXT
            );
        """)

    def tearDown(self):
        self.conn.close()

    @patch("sales._refund_total", return_value=Decimal("0"))
    @patch("sales._account", return_value=2)
    @patch("sales._sales_account_codes", return_value={"ar": "1100"})
    @patch("sales._activity", return_value="commercial")
    @patch("sales.post_journal", return_value=777)
    def test_one_receipt_is_allocated_to_multiple_invoices(self, *_mocks):
        result = post_customer_receipt_batch(
            self.conn, 1, "2026-11-11", [(11, "300"), (12, "150")], 1, 5, "BANK-01"
        )
        self.assertEqual(result["total_amount"], Decimal("450.00"))
        self.assertEqual(result["invoice_count"], 2)
        self.assertEqual(self.conn.execute(
            "SELECT paid_amount FROM sales_invoices WHERE id=11"
        ).fetchone()["paid_amount"], "500.00")
        self.assertEqual(self.conn.execute(
            "SELECT paid_amount FROM sales_invoices WHERE id=12"
        ).fetchone()["paid_amount"], "150.00")
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) n FROM customer_receipt_allocations"
        ).fetchone()["n"], 2)

    @patch("sales._refund_total", return_value=Decimal("0"))
    @patch("sales._account", return_value=2)
    @patch("sales._sales_account_codes", return_value={"ar": "1100"})
    @patch("sales._activity", return_value="commercial")
    @patch("sales.post_journal", return_value=778)
    def test_receipt_cannot_exceed_invoice_remaining_balance(self, *_mocks):
        with self.assertRaises(ValueError):
            post_customer_receipt_batch(
                self.conn, 1, "2026-11-11", [(11, "801")], 1, 5, ""
            )
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) n FROM customer_receipt_batches"
        ).fetchone()["n"], 0)


if __name__ == "__main__":
    unittest.main()
