import sqlite3
import tempfile
import unittest
from pathlib import Path

from flask import Flask
import tax_center


class TaxCenterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "nexus.db"
        self.original_path = tax_center.DB_PATH
        tax_center.DB_PATH = self.db_path
        conn = sqlite3.connect(self.db_path)
        conn.executescript("""
          CREATE TABLE company_settings(setting_key TEXT PRIMARY KEY, setting_value TEXT NOT NULL);
          CREATE TABLE customers(id INTEGER PRIMARY KEY, name TEXT NOT NULL);
          CREATE TABLE suppliers(id INTEGER PRIMARY KEY, name TEXT NOT NULL);
          CREATE TABLE sales_invoices(
            id INTEGER PRIMARY KEY, invoice_no TEXT, customer_id INTEGER, invoice_date TEXT,
            subtotal TEXT, tax_rate TEXT, tax_amount TEXT, total_amount TEXT, status TEXT);
          CREATE TABLE supplier_bills(
            id INTEGER PRIMARY KEY, bill_no TEXT, supplier_id INTEGER, bill_date TEXT,
            subtotal TEXT, tax_rate TEXT, tax_amount TEXT, total_amount TEXT, status TEXT);
          CREATE TABLE audit_log(id INTEGER PRIMARY KEY, user_id INTEGER, action TEXT, details TEXT, created_at TEXT);
          INSERT INTO customers VALUES(1,'Customer A');
          INSERT INTO suppliers VALUES(1,'Supplier A');
          INSERT INTO company_settings VALUES('tax_country','EG');
          INSERT INTO sales_invoices VALUES(1,'SI-1',1,'2026-11-05','1000','14','140','1140','posted');
          INSERT INTO sales_invoices VALUES(2,'SI-2',1,'2026-11-06','500','0','0','500','posted');
          INSERT INTO supplier_bills VALUES(1,'PB-1',1,'2026-11-07','400','14','56','456','posted');
        """)
        conn.commit()
        conn.close()
        tax_center.initialize_tax_schema()
        template_dir = Path(self.temp.name) / "templates"
        template_dir.mkdir()
        (template_dir / "tax_report.html").write_text(
            "{{ '%.2f'|format(summary.output_vat) }}|{{ '%.2f'|format(summary.input_vat) }}|مبيعات بسعر صفر",
            encoding="utf-8"
        )
        self.app = Flask(__name__, template_folder=str(template_dir))
        self.app.secret_key = "test"
        self.app.register_blueprint(tax_center.tax_bp)
        self.client = self.app.test_client()

    def tearDown(self):
        tax_center.DB_PATH = self.original_path
        self.temp.cleanup()

    def login(self, role="accountant"):
        with self.client.session_transaction() as session:
            session["user_id"] = 1
            session["role"] = role

    def test_additive_schema_migration(self):
        conn = sqlite3.connect(self.db_path)
        sales_cols = {r[1] for r in conn.execute("PRAGMA table_info(sales_invoices)")}
        purchase_cols = {r[1] for r in conn.execute("PRAGMA table_info(supplier_bills)")}
        conn.close()
        self.assertIn("tax_treatment", sales_cols)
        self.assertIn("tax_reference", sales_cols)
        self.assertIn("input_vat_deductible_percent", purchase_cols)

    def test_monthly_report_shows_vat_totals_and_classifications(self):
        self.login()
        response = self.client.get("/reports/tax?period=2026-11")
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("140.00", body)
        self.assertIn("56.00", body)
        self.assertIn("مبيعات بسعر صفر", body)

    def test_posted_purchase_tax_classification_is_locked(self):
        self.login()
        response = self.client.post("/reports/tax/classify/purchase/1", data={
            "period": "2026-11", "tax_treatment": "standard",
            "deductible_percent": "75", "tax_reference": "INV-REF"
        }, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        conn = sqlite3.connect(self.db_path)
        value = conn.execute("SELECT input_vat_deductible_percent FROM supplier_bills WHERE id=1").fetchone()[0]
        audit_count = conn.execute("SELECT COUNT(*) FROM audit_log WHERE action='tax_classification'").fetchone()[0]
        conn.close()
        self.assertEqual(value, "100")
        self.assertEqual(audit_count, 0)
        self.assertEqual(response.status_code, 200)

    def test_viewer_cannot_change_tax_classification(self):
        self.login("viewer")
        response = self.client.post("/reports/tax/classify/sale/1", data={
            "period": "2026-11", "tax_treatment": "zero_rated"
        }, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        conn = sqlite3.connect(self.db_path)
        value = conn.execute("SELECT tax_treatment FROM sales_invoices WHERE id=1").fetchone()[0]
        conn.close()
        self.assertEqual(value, "standard")


if __name__ == "__main__":
    unittest.main()
