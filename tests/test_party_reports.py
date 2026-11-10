import sqlite3
import tempfile
import unittest
from pathlib import Path

from flask import Flask
import party_reports


class PartyStatementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "nexus.db"
        self.original_path = party_reports.DB_PATH
        party_reports.DB_PATH = self.db_path
        conn = sqlite3.connect(self.db_path)
        conn.executescript("""
          CREATE TABLE customers(id INTEGER PRIMARY KEY, code TEXT, name TEXT, active INTEGER DEFAULT 1);
          CREATE TABLE suppliers(id INTEGER PRIMARY KEY, code TEXT, name TEXT, active INTEGER DEFAULT 1);
          CREATE TABLE sales_invoices(id INTEGER PRIMARY KEY, customer_id INTEGER, invoice_no TEXT,
            invoice_date TEXT, total_amount TEXT, status TEXT);
          CREATE TABLE sales_credit_notes(id INTEGER PRIMARY KEY, invoice_id INTEGER, credit_note_no TEXT,
            credit_date TEXT, reason TEXT, total_amount TEXT, status TEXT);
          CREATE TABLE customer_receipts(id INTEGER PRIMARY KEY, invoice_id INTEGER, receipt_no TEXT,
            receipt_date TEXT, amount TEXT, reference TEXT);
          CREATE TABLE supplier_bills(id INTEGER PRIMARY KEY, supplier_id INTEGER, bill_no TEXT,
            bill_date TEXT, total_amount TEXT, status TEXT);
          CREATE TABLE supplier_payments(id INTEGER PRIMARY KEY, supplier_id INTEGER, payment_no TEXT,
            payment_date TEXT, amount TEXT, reference TEXT);
          INSERT INTO customers VALUES(1,'C001','Customer',1);
          INSERT INTO suppliers VALUES(1,'S001','Supplier',1);
          INSERT INTO sales_invoices VALUES(1,1,'SI-OLD','2026-10-10','1000','posted');
          INSERT INTO sales_invoices VALUES(2,1,'SI-NOW','2026-11-10','500','posted');
          INSERT INTO customer_receipts VALUES(1,1,'CR-OLD','2026-10-20','200','');
          INSERT INTO customer_receipts VALUES(2,2,'CR-NOW','2026-11-20','100','');
          INSERT INTO supplier_bills VALUES(1,1,'PB-OLD','2026-10-10','2000','posted');
          INSERT INTO supplier_bills VALUES(2,1,'PB-NOW','2026-11-10','400','posted');
          INSERT INTO supplier_payments VALUES(1,1,'SP-OLD','2026-10-20','500','');
          INSERT INTO supplier_payments VALUES(2,1,'SP-NOW','2026-11-20','200','');
        """)
        conn.commit()
        conn.close()
        template_dir = Path(self.temp.name) / "templates"
        template_dir.mkdir()
        (template_dir / "party_statement.html").write_text(
            "{{ kind }}|{{ opening }}|{{ closing }}|{{ rows|length }}", encoding="utf-8"
        )
        self.app = Flask(__name__, template_folder=str(template_dir))
        self.app.secret_key = "test"
        self.app.register_blueprint(party_reports.party_bp)
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session["user_id"] = 1
            session["role"] = "accountant"

    def tearDown(self):
        party_reports.DB_PATH = self.original_path
        self.temp.cleanup()

    def test_customer_statement_opening_and_running_balance(self):
        response = self.client.get("/reports/party-statement?kind=customer&party_id=1&date_from=2026-11-01&date_to=2026-11-30")
        self.assertEqual(response.status_code, 200)
        self.assertIn("customer|800.00|1200.00|2", response.get_data(as_text=True))

    def test_supplier_statement_opening_and_running_balance(self):
        response = self.client.get("/reports/party-statement?kind=supplier&party_id=1&date_from=2026-11-01&date_to=2026-11-30")
        self.assertEqual(response.status_code, 200)
        self.assertIn("supplier|1500.00|1700.00|2", response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
