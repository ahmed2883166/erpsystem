import sqlite3
import tempfile
import unittest
from unittest.mock import patch
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
          INSERT INTO company_settings VALUES('company_name','Test Co');
          INSERT INTO company_settings VALUES('tax_number','123456789');
          CREATE TABLE customers(id INTEGER PRIMARY KEY, name TEXT NOT NULL, tax_number TEXT NOT NULL DEFAULT '');
          CREATE TABLE suppliers(id INTEGER PRIMARY KEY, name TEXT NOT NULL, tax_number TEXT NOT NULL DEFAULT '');
          CREATE TABLE sales_invoices(
            id INTEGER PRIMARY KEY, invoice_no TEXT, customer_id INTEGER, invoice_date TEXT,
            subtotal TEXT, tax_rate TEXT, tax_amount TEXT, total_amount TEXT, status TEXT);
          CREATE TABLE sales_credit_notes(
            id INTEGER PRIMARY KEY, credit_note_no TEXT, invoice_id INTEGER, credit_date TEXT,
            reason TEXT, total_amount TEXT, status TEXT);
          CREATE TABLE sales_credit_note_lines(
            id INTEGER PRIMARY KEY, credit_note_id INTEGER, line_total TEXT, tax_amount TEXT,
            tax_treatment TEXT);
          CREATE TABLE supplier_bills(
            id INTEGER PRIMARY KEY, bill_no TEXT, supplier_id INTEGER, bill_date TEXT,
            subtotal TEXT, tax_rate TEXT, tax_amount TEXT, total_amount TEXT, status TEXT);
          CREATE TABLE audit_log(id INTEGER PRIMARY KEY, user_id INTEGER, action TEXT, details TEXT, created_at TEXT);
          INSERT INTO customers(id,name,tax_number) VALUES(1,'Customer A','123456789');
          INSERT INTO suppliers(id,name,tax_number) VALUES(1,'Supplier A','123456789');
          INSERT INTO company_settings VALUES('tax_country','EG');
          INSERT INTO sales_invoices VALUES(1,'SI-1',1,'2026-11-05','1000','14','140','1140','posted');
          INSERT INTO sales_invoices VALUES(2,'SI-2',1,'2026-11-06','500','0','0','500','posted');
          INSERT INTO sales_credit_notes VALUES(1,'SCN-1',1,'2026-11-20','Return','22.80','posted');
          INSERT INTO sales_credit_note_lines VALUES(1,1,'20.00','2.80','standard');
          INSERT INTO supplier_bills VALUES(1,'PB-1',1,'2026-11-07','400','14','56','456','posted');
        """)
        conn.commit()
        conn.close()
        tax_center.initialize_tax_schema()
        template_dir = Path(self.temp.name) / "templates"
        template_dir.mkdir()
        (template_dir / "tax_report.html").write_text(
            "{{ '%.2f'|format(summary.output_vat) }}|{{ '%.2f'|format(summary.input_vat) }}|{% for row in sales %}{{ '%.2f'|format(row.tax_amount) }}|{{ '%.2f'|format(row.taxable_base) }}|{% endfor %}مبيعات بسعر صفر",
            encoding="utf-8"
        )
        self.app = Flask(__name__, template_folder=str(template_dir))
        self.app.add_url_rule("/__test_global_search", endpoint="global_search", view_func=lambda: "")
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
        self.assertIn("137.20", body)
        self.assertIn("-2.80", body)
        self.assertIn("140.00", body)
        self.assertIn("56.00", body)
        self.assertIn("مبيعات بسعر صفر", body)

    def test_quality_report_flags_missing_party_tax_number_and_bad_amounts(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE customers SET tax_number='' WHERE id=1")
        conn.execute("UPDATE sales_invoices SET tax_amount='150' WHERE id=1")
        conn.commit()
        conn.close()
        snapshot_conn = tax_center._db()
        try:
            snapshot = tax_center.build_tax_report_snapshot(snapshot_conn, "2026-11")
        finally:
            snapshot_conn.close()
        issues = snapshot["quality_issues"]
        self.assertTrue(any("الرقم الضريبي للطرف" in x["issue"] for x in issues))
        self.assertTrue(any("فرق بين الضريبة المحسوبة" in x["issue"] for x in issues))

    def test_tax_ledger_reconciliation_matches_posted_control_accounts(self):
        conn = sqlite3.connect(self.db_path)
        conn.executescript("""
          CREATE TABLE accounts(id INTEGER PRIMARY KEY, code TEXT, name_ar TEXT, account_type TEXT,
                                is_group INTEGER DEFAULT 0, active INTEGER DEFAULT 1);
          CREATE TABLE journal_entries(id INTEGER PRIMARY KEY, entry_date TEXT, status TEXT);
          CREATE TABLE journal_lines(id INTEGER PRIMARY KEY, entry_id INTEGER, account_id INTEGER,
                                     debit TEXT DEFAULT '0', credit TEXT DEFAULT '0');
          INSERT INTO accounts VALUES(1,'2130','ضريبة مخرجات','liability',0,1);
          INSERT INTO accounts VALUES(2,'1165','ضريبة مدخلات','asset',0,1);
          INSERT INTO journal_entries VALUES(1,'2026-11-05','posted');
          INSERT INTO journal_entries VALUES(2,'2026-11-07','posted');
          INSERT INTO journal_lines VALUES(1,1,1,'0','137.20');
          INSERT INTO journal_lines VALUES(2,2,2,'56','0');
        """)
        conn.commit()
        conn.close()
        conn = tax_center._db()
        try:
            snapshot = tax_center.build_tax_report_snapshot(conn, "2026-11")
        finally:
            conn.close()
        self.assertTrue(snapshot["ledger_reconciliation"]["available"])
        self.assertEqual(snapshot["ledger_reconciliation"]["output_difference"], 0)
        self.assertEqual(snapshot["ledger_reconciliation"]["input_difference"], 0)

    def test_saudi_annual_tax_workpaper_uses_accounting_profit_without_calculating_tax(self):
        conn = tax_center._db()
        try:
            conn.execute("UPDATE company_settings SET setting_value='SA' WHERE setting_key='tax_country'")
            conn.commit()
            with patch("income_statement.build_income_statement", return_value={
                "revenue_total": 120000, "cogs_total": 45000, "gross_profit": 75000,
                "operating_expense_total": 25000, "net_profit": 50000,
            }) as build_statement, patch("financial_planning.build_balance_sheet_snapshot", return_value={
                "total_assets": 200000, "total_liabilities": 90000,
                "equity_before_profit": 60000, "total_equity": 110000,
                "balance_difference": 0, "balanced": True, "negative_asset_accounts": [],
            }) as build_balance:
                snapshot = tax_center.build_tax_report_snapshot(conn, "2026-11")
            build_statement.assert_called_once_with(conn, "2026-01-01", "2026-12-31")
            build_balance.assert_called_once_with(conn, "2026-12-31")
        finally:
            conn.close()
        prep = snapshot["saudi_preparation"]
        self.assertTrue(prep["available"])
        self.assertEqual(prep["net_profit"], 50000)
        self.assertEqual(prep["total_assets"], 200000)
        self.assertEqual(prep["total_liabilities"], 90000)
        self.assertTrue(prep["balance_sheet_balanced"])
        self.assertIsNone(prep["zakat_or_income_tax"])
        self.assertIn("لم يتم احتساب", prep["notice"])

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
