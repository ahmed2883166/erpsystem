import sqlite3
import tempfile
import unittest
from pathlib import Path

from flask import Flask

import sales
import purchases


class InvoicePrintRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "invoices.db"
        self.old_sales_path = sales.DB_PATH
        self.old_purchases_path = purchases.DB_PATH
        sales.DB_PATH = self.db_path
        purchases.DB_PATH = self.db_path

        conn = sqlite3.connect(self.db_path)
        conn.executescript("""
          CREATE TABLE customers(id INTEGER PRIMARY KEY,code TEXT,name TEXT,tax_number TEXT);
          CREATE TABLE suppliers(id INTEGER PRIMARY KEY,code TEXT,name TEXT,tax_number TEXT);
          CREATE TABLE stock_products(id INTEGER PRIMARY KEY,sku TEXT,name_ar TEXT);
          CREATE TABLE sales_invoices(
            id INTEGER PRIMARY KEY,invoice_no TEXT,customer_id INTEGER,invoice_date TEXT,due_date TEXT,
            subtotal TEXT,tax_amount TEXT,total_amount TEXT,paid_amount TEXT,credited_amount TEXT,
            status TEXT,journal_entry_id INTEGER
          );
          CREATE TABLE sales_invoice_lines(
            id INTEGER PRIMARY KEY,invoice_id INTEGER,product_id INTEGER,description TEXT,
            quantity TEXT,unit_price TEXT,line_total TEXT
          );
          CREATE TABLE supplier_bills(
            id INTEGER PRIMARY KEY,bill_no TEXT,supplier_id INTEGER,bill_date TEXT,due_date TEXT,
            supplier_invoice_no TEXT,subtotal TEXT,tax_amount TEXT,total_amount TEXT,
            paid_amount TEXT,credited_amount TEXT,journal_entry_id INTEGER,status TEXT
          );
          CREATE TABLE accounts(id INTEGER PRIMARY KEY,code TEXT,name_ar TEXT);
          CREATE TABLE journal_lines(id INTEGER PRIMARY KEY,entry_id INTEGER,account_id INTEGER,debit TEXT,credit TEXT,memo TEXT);
          CREATE TABLE supplier_bill_receipts(bill_id INTEGER,receipt_id INTEGER,matched_amount TEXT);
          CREATE TABLE goods_receipts(id INTEGER PRIMARY KEY,receipt_no TEXT,receipt_date TEXT,total_value TEXT);
          CREATE TABLE company_settings(setting_key TEXT PRIMARY KEY,setting_value TEXT);
          INSERT INTO customers VALUES(1,'C-1','عميل اختبار','');
          INSERT INTO suppliers VALUES(1,'S-1','مورد اختبار','');
          INSERT INTO stock_products VALUES(1,'P-1','صنف اختبار');
          INSERT INTO sales_invoices VALUES
            (1,'SI-ROUTE',1,'2026-11-10','2026-11-20','100','14','114','20','0','posted',1);
          INSERT INTO sales_invoice_lines VALUES(1,1,1,'صنف اختبار','2','50','100');
          INSERT INTO supplier_bills VALUES
            (1,'PB-ROUTE',1,'2026-11-10','2026-11-20','SUP-100','100','14','114','0','0',2,'posted');
          INSERT INTO company_settings VALUES('company_name','NEXUS ERP');
          INSERT INTO company_settings VALUES('base_currency','EGP');
        """)
        conn.commit()
        conn.close()

        templates = Path(__file__).resolve().parents[1] / "templates"
        self.app = Flask(__name__, template_folder=str(templates))
        self.app.secret_key = "test"
        self.app.jinja_env.globals["csrf_token"] = lambda: "test-token"
        self.app.jinja_env.filters["money"] = lambda value: f"{float(value or 0):,.2f}"
        self.app.context_processor(lambda: {
            "permissions": {"sales.view", "purchases.view"},
            "license_info": {"status": "demo"},
        })
        self.app.add_url_rule("/change-password", endpoint="change_password", view_func=lambda: "")
        self.app.add_url_rule("/logout", endpoint="logout", view_func=lambda: "")
        self.app.register_blueprint(sales.sales_bp)
        self.app.register_blueprint(purchases.purchase_bp)
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session.update(user_id=1, username="tester", full_name="اختبار",
                           role="accountant", permissions=["sales.view", "purchases.view"])

    def tearDown(self):
        sales.DB_PATH = self.old_sales_path
        purchases.DB_PATH = self.old_purchases_path
        self.temp.cleanup()

    def test_sales_invoice_print_route_uses_real_database_and_template(self):
        response = self.client.get("/sales/invoices/1/print")
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("SI-ROUTE", body)
        self.assertIn("عميل اختبار", body)
        self.assertIn("114.00", body)

    def test_supplier_bill_print_route_uses_real_database_and_template(self):
        response = self.client.get("/purchases/bills/1/print")
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("PB-ROUTE", body)
        self.assertIn("مورد اختبار", body)
        self.assertIn("114.00", body)


if __name__ == "__main__":
    unittest.main()
