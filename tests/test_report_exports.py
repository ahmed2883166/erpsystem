import io
import sqlite3
import tempfile
import unittest
from pathlib import Path

from flask import Flask
from openpyxl import load_workbook

from report_exports import register_report_exports


class TrialBalanceExcelExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "reports.db"
        conn = sqlite3.connect(self.db_path)
        conn.executescript("""
            CREATE TABLE company_settings(setting_key TEXT PRIMARY KEY, setting_value TEXT NOT NULL);
            CREATE TABLE accounts(
                id INTEGER PRIMARY KEY, code TEXT, name_ar TEXT, account_type TEXT,
                parent_id INTEGER, is_group INTEGER DEFAULT 0, active INTEGER DEFAULT 1
            );
            CREATE TABLE financial_years(
                id INTEGER PRIMARY KEY, year_name TEXT, start_date TEXT, end_date TEXT, status TEXT
            );
            CREATE TABLE journal_entries(
                id INTEGER PRIMARY KEY, entry_date TEXT, status TEXT
            );
            CREATE TABLE management_account_roles(account_id INTEGER, role TEXT);
            CREATE TABLE management_dashboard_meta(meta_key TEXT PRIMARY KEY, meta_value TEXT);

            CREATE TABLE journal_lines(
                id INTEGER PRIMARY KEY, entry_id INTEGER, account_id INTEGER,
                debit TEXT DEFAULT '0', credit TEXT DEFAULT '0'
            );
            CREATE TABLE customers(id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE suppliers(id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE sales_invoices(
                id INTEGER PRIMARY KEY, invoice_no TEXT, invoice_date TEXT, customer_id INTEGER,
                subtotal TEXT, tax_rate TEXT, tax_amount TEXT, total_amount TEXT,
                tax_treatment TEXT, tax_reference TEXT, tax_notes TEXT, status TEXT
            );
            CREATE TABLE supplier_bills(
                id INTEGER PRIMARY KEY, bill_no TEXT, bill_date TEXT, supplier_id INTEGER,
                subtotal TEXT, tax_rate TEXT, tax_amount TEXT, total_amount TEXT,
                tax_treatment TEXT, tax_reference TEXT, tax_notes TEXT,
                input_vat_deductible_percent TEXT, status TEXT
            );
            INSERT INTO customers VALUES(1,'عميل ضريبي');
            INSERT INTO suppliers VALUES(1,'مورد ضريبي');
            INSERT INTO sales_invoices VALUES
                (1,'INV-TAX','2026-06-10',1,'100','14','14','114','standard','','','posted');
            INSERT INTO supplier_bills VALUES
                (1,'BILL-TAX','2026-06-11',1,'50','14','7','57','standard','','','80','posted');

            INSERT INTO company_settings VALUES('company_name','NEXUS Test');
            INSERT INTO accounts(id,code,name_ar,account_type) VALUES
                (1,'1110','الصندوق','asset'),
                (2,'4100','إيرادات المبيعات','revenue');
            INSERT INTO financial_years VALUES
                (1,'2025','2025-01-01','2025-12-31','closed'),
                (2,'2026','2026-01-01','2026-12-31','open');
            INSERT INTO journal_entries VALUES
                (1,'2025-06-01','posted'),
                (2,'2026-06-01','posted'),
                (3,'2026-07-01','draft');
            INSERT INTO journal_lines VALUES
                (1,1,1,'100','0'),
                (2,1,2,'0','100'),
                (3,2,1,'250','0'),
                (4,2,2,'0','250'),
                (5,3,1,'999','0');
        """)
        conn.commit()
        conn.close()

        self.app = Flask(__name__)
        self.app.secret_key = "test"
        register_report_exports(self.app, self.db_path, {"admin": {"reports.view"}})
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session.update(user_id=1, role="admin", permissions=["reports.view"])

    def tearDown(self):
        self.temp.cleanup()

    def test_trial_balance_export_separates_closed_and_open_years(self):
        response = self.client.get("/reports/export/trial-balance/excel")
        self.assertEqual(response.status_code, 200)
        workbook = load_workbook(io.BytesIO(response.data), data_only=True)
        sheet = workbook.active
        values = list(sheet.values)
        rows = [row for row in values if row and row[4] in ("1110", "4100")]
        self.assertEqual(len(rows), 4)
        by_period = {}
        for row in rows:
            by_period.setdefault(row[0], []).append(row)
        self.assertEqual(set(by_period), {"2025", "2026"})
        self.assertEqual(by_period["2025"][0][7], 100)
        self.assertEqual(by_period["2026"][0][7], 250)
        self.assertNotIn(999, [cell for row in rows for cell in row])

    def test_trial_balance_export_applies_date_filter_to_each_year(self):
        response = self.client.get(
            "/reports/export/trial-balance/excel?date_from=2026-01-01&date_to=2026-06-30"
        )
        self.assertEqual(response.status_code, 200)
        workbook = load_workbook(io.BytesIO(response.data), data_only=True)
        values = list(workbook.active.values)
        account_rows = [row for row in values if row and row[4] in ("1110", "4100")]
        self.assertEqual(len(account_rows), 2)
        self.assertTrue(all(row[0] == "2026" for row in account_rows))

    def test_trial_balance_pdf_export_returns_pdf_document(self):
        response = self.client.get("/reports/export/trial-balance/pdf")
        self.assertEqual(response.status_code, 200, response.data[:300].decode("utf-8", errors="replace"))
        self.assertEqual(response.mimetype, "application/pdf")
        self.assertTrue(response.data.startswith(b"%PDF"))

    def test_balance_sheet_export_uses_snapshot_and_selected_date(self):
        response = self.client.get("/reports/export/balance-sheet/excel?as_of=2026-06-30")
        self.assertEqual(response.status_code, 200)
        workbook = load_workbook(io.BytesIO(response.data), data_only=True)
        values = list(workbook.active.values)
        self.assertTrue(any(row[2] == "إجمالي الأصول" and row[3] == 350 for row in values if row[2]))

    def test_balance_sheet_pdf_export_returns_pdf_document(self):
        response = self.client.get("/reports/export/balance-sheet/pdf?as_of=2026-06-30")
        self.assertEqual(response.status_code, 200, response.data[:200].decode("utf-8", errors="replace"))
        self.assertEqual(response.mimetype, "application/pdf")
        self.assertTrue(response.data.startswith(b"%PDF"))

    def test_cash_flow_export_uses_cash_flow_snapshot(self):
        response = self.client.get(
            "/reports/export/cash-flow/excel?date_from=2026-01-01&date_to=2026-12-31"
        )
        self.assertEqual(response.status_code, 200)
        workbook = load_workbook(io.BytesIO(response.data), data_only=True)
        values = list(workbook.active.values)
        self.assertTrue(any(row[0] == "الأنشطة التشغيلية" and row[1] == 250 for row in values if row[0]))

    def test_cash_flow_pdf_export_returns_pdf_document(self):
        response = self.client.get(
            "/reports/export/cash-flow/pdf?date_from=2026-01-01&date_to=2026-12-31"
        )
        self.assertEqual(response.status_code, 200, response.data[:200].decode("utf-8", errors="replace"))
        self.assertEqual(response.mimetype, "application/pdf")
        self.assertTrue(response.data.startswith(b"%PDF"))

    def test_monthly_tax_report_excel_export_includes_vat_summary(self):
        response = self.client.get("/reports/export/tax-report/excel?period=2026-06")
        self.assertEqual(response.status_code, 200)
        workbook = load_workbook(io.BytesIO(response.data), data_only=True)
        values = list(workbook.active.values)
        self.assertTrue(any(row[3] == "ضريبة المخرجات" and row[7] == 14 for row in values if row[3]))

    def test_monthly_tax_report_pdf_export_returns_pdf_document(self):
        response = self.client.get("/reports/export/tax-report/pdf?period=2026-06")
        self.assertEqual(response.status_code, 200, response.data[:200].decode("utf-8", errors="replace"))
        self.assertEqual(response.mimetype, "application/pdf")
        self.assertTrue(response.data.startswith(b"%PDF"))

    def test_trial_balance_export_rejects_reversed_date_range(self):
        response = self.client.get(
            "/reports/export/trial-balance/excel?date_from=2026-12-31&date_to=2026-01-01"
        )
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
