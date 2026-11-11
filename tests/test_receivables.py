import sqlite3
import tempfile
import unittest
from pathlib import Path
from flask import Flask

import receivables


class ReceivablesAgingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original_path = receivables.DB_PATH
        receivables.DB_PATH = Path(self.temp.name) / "nexus.db"
        conn = sqlite3.connect(receivables.DB_PATH)
        conn.executescript("""
          CREATE TABLE customers(id INTEGER PRIMARY KEY,code TEXT,name TEXT,active INTEGER);
          CREATE TABLE suppliers(id INTEGER PRIMARY KEY,code TEXT,name TEXT,active INTEGER);
          CREATE TABLE sales_invoices(id INTEGER PRIMARY KEY,customer_id INTEGER,invoice_no TEXT,
            invoice_date TEXT,due_date TEXT,total_amount TEXT,paid_amount TEXT,credited_amount TEXT,status TEXT);
          CREATE TABLE sales_credit_notes(id INTEGER PRIMARY KEY,invoice_id INTEGER,status TEXT);
          CREATE TABLE customer_refunds(id INTEGER PRIMARY KEY,credit_note_id INTEGER,refund_date TEXT,amount TEXT);
          CREATE TABLE supplier_bills(id INTEGER PRIMARY KEY,supplier_id INTEGER,bill_no TEXT,
            bill_date TEXT,due_date TEXT,total_amount TEXT,paid_amount TEXT,status TEXT);
          INSERT INTO customers VALUES(1,'C1','عميل',1);
          INSERT INTO suppliers VALUES(1,'S1','مورد',1);
          INSERT INTO sales_invoices VALUES(1,1,'SI-1','2026-11-01','2026-11-10','100','50','20','posted');
          INSERT INTO sales_credit_notes VALUES(1,1,'posted');
          INSERT INTO customer_refunds VALUES(1,1,'2026-11-20','10');
          INSERT INTO customer_refunds VALUES(2,1,'2026-12-01','5');
          INSERT INTO supplier_bills VALUES(1,1,'PB-1','2026-11-01','2026-11-10','200','50','posted');
        """)
        conn.commit()
        conn.close()
        self.app = Flask(__name__)
        self.app.secret_key = "test"
        self.app.register_blueprint(receivables.aging_bp)
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session["user_id"] = 1
            session["role"] = "accountant"

    def tearDown(self):
        receivables.DB_PATH = self.original_path
        self.temp.cleanup()

    def test_customer_aging_uses_only_refunds_as_of_report_date(self):
        response = self.client.get("/reports/aging/?kind=customers&as_of=2026-11-30")
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("40", body)
        self.assertNotIn("45.00", body)

    def test_supplier_aging_does_not_require_customer_invoice_fields(self):
        response = self.client.get("/reports/aging/?kind=suppliers&as_of=2026-11-30")
        self.assertEqual(response.status_code, 200)
        self.assertIn("150", response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
