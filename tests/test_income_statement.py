import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

import app as nexus_app
from accounting_core import register_decimal_aggregates
from income_statement import (build_income_statement, normalize_income_statement_period,
                              comparison_period)


class IncomeStatementRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "income.db"
        conn = sqlite3.connect(self.db_path)
        conn.executescript("""
            CREATE TABLE accounts(
                id INTEGER PRIMARY KEY, code TEXT, name_ar TEXT,
                account_type TEXT, is_group INTEGER DEFAULT 0, active INTEGER DEFAULT 1
            );
            CREATE TABLE journal_entries(
                id INTEGER PRIMARY KEY, entry_date TEXT, status TEXT,
                entry_type TEXT NOT NULL DEFAULT 'general'
            );
            CREATE TABLE journal_lines(
                id INTEGER PRIMARY KEY, entry_id INTEGER, account_id INTEGER,
                debit TEXT, credit TEXT
            );
            CREATE TABLE company_settings(setting_key TEXT PRIMARY KEY, setting_value TEXT);
            CREATE TABLE management_account_roles(account_id INTEGER, role TEXT);
            CREATE TABLE financial_years(
                id INTEGER PRIMARY KEY, year_name TEXT, start_date TEXT, end_date TEXT, status TEXT
            );
            INSERT INTO financial_years VALUES(1,'2026','2026-01-01','2026-12-31','open');
            INSERT INTO accounts VALUES
                (1,'4100','إيرادات المبيعات','revenue',0,1),
                (2,'4110','مردودات المبيعات','revenue',0,1),
                (3,'5100','تكلفة البضاعة المباعة','expense',0,1),
                (4,'6100','مصروفات التشغيل','expense',0,1),
                (5,'3200','الأرباح المرحلة','equity',0,1);
            INSERT INTO management_account_roles VALUES(3,'direct_cost');
            INSERT INTO journal_entries(id,entry_date,status,entry_type) VALUES
                (1,'2026-11-10','posted','general'),
                (2,'2026-11-11','posted','general'),
                (3,'2026-11-12','posted','general'),
                (4,'2026-11-12','posted','general'),
                (5,'2026-11-12','posted','general'),
                (6,'2026-11-12','draft','general'),
                (7,'2026-11-12','posted','year_end_closing'),
                (8,'2025-11-10','posted','general'),
                (9,'2025-11-11','posted','general'),
                (10,'2025-11-12','posted','general');
            INSERT INTO journal_lines VALUES
                (1,1,1,'0','1000'),
                (2,2,2,'100','0'),
                (3,3,3,'200','0'),
                (4,4,4,'300','0'),
                (5,5,4,'0','50'),
                (6,6,1,'0','500'),
                (7,7,1,'1000','0'),
                (8,7,2,'0','100'),
                (9,7,3,'0','200'),
                (10,7,4,'0','250'),
                (11,7,5,'0','450'),
                (12,8,1,'0','800'),
                (13,9,3,'150','0'),
                (14,10,4,'180','0');
            INSERT INTO company_settings VALUES('base_currency','EGP');
            INSERT INTO company_settings VALUES('company_name','شركة الاختبار');
            CREATE TABLE branches(id INTEGER PRIMARY KEY,code TEXT,name_ar TEXT,active INTEGER DEFAULT 1);
            CREATE TABLE cost_centers(id INTEGER PRIMARY KEY,code TEXT,name_ar TEXT,active INTEGER DEFAULT 1);
            CREATE TABLE projects(id INTEGER PRIMARY KEY,code TEXT,name_ar TEXT,status TEXT DEFAULT 'active',active INTEGER DEFAULT 1);
            CREATE TABLE customers(id INTEGER PRIMARY KEY,code TEXT,name TEXT,active INTEGER DEFAULT 1);
            CREATE TABLE salespersons(id INTEGER PRIMARY KEY,code TEXT,name TEXT,active INTEGER DEFAULT 1);
            CREATE TABLE suppliers(id INTEGER PRIMARY KEY,code TEXT,name TEXT,active INTEGER DEFAULT 1);
            INSERT INTO branches VALUES(1,'B1','فرع القاهرة',1);
            INSERT INTO cost_centers VALUES(1,'CC1','مركز تجاري',1);
            INSERT INTO projects VALUES(1,'P1','مشروع ألف','active',1);
            INSERT INTO customers VALUES(1,'C1','عميل ألف',1);
            INSERT INTO salespersons VALUES(1,'SP1','مندوب ألف',1);
            INSERT INTO suppliers VALUES(1,'S1','مورد ألف',1);
            ALTER TABLE journal_lines ADD COLUMN branch_id INTEGER;
            ALTER TABLE journal_lines ADD COLUMN cost_center_id INTEGER;
            ALTER TABLE journal_lines ADD COLUMN project_id INTEGER;
            ALTER TABLE journal_lines ADD COLUMN customer_id INTEGER;
            ALTER TABLE journal_lines ADD COLUMN salesperson_id INTEGER;
            ALTER TABLE journal_lines ADD COLUMN supplier_id INTEGER;
            UPDATE journal_lines SET branch_id=1,project_id=1,customer_id=1,salesperson_id=1 WHERE id IN (1,3);
        """)
        conn.commit()
        conn.close()
        self.original_db = nexus_app.db

        def connect():
            connection = sqlite3.connect(self.db_path)
            connection.row_factory = sqlite3.Row
            register_decimal_aggregates(connection)
            return connection

        nexus_app.db = connect
        self.client = nexus_app.app.test_client()
        with self.client.session_transaction() as session:
            session["user_id"] = 1
            session["role"] = "admin"
            session["permissions"] = ["reports.view", "expenses.view"]

    def tearDown(self):
        nexus_app.db = self.original_db
        self.temp.cleanup()

    def test_expense_analysis_requires_dedicated_expenses_view_permission(self):
        with self.client.session_transaction() as session:
            session["permissions"] = ["reports.view"]
        response = self.client.get("/reports/expense-analysis")
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as session:
            session["permissions"] = ["reports.view", "expenses.view"]

    def test_analytical_expense_report_route_renders_filtered_report(self):
        response = self.client.get(
            "/reports/expense-analysis?date_from=2026-11-01&date_to=2026-11-30"
        )
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("تقرير المصروفات التحليلي", body)
        self.assertIn("مصروفات التشغيل", body)
        self.assertIn("250.00", body)


    def test_income_statement_excludes_closing_and_draft_entries_and_separates_cogs(self):
        response = self.client.get(
            "/reports/income-statement?date_from=2026-11-01&date_to=2026-11-30"
        )
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("900.00", body)  # Net revenue
        self.assertIn("200.00", body)  # Cost of goods sold
        self.assertIn("700.00", body)  # Gross profit
        self.assertIn("250.00", body)  # Operating expenses
        self.assertIn("450.00", body)  # Net profit after operating expenses
        self.assertIn("شركة الاختبار", body)

    def test_income_statement_compares_with_same_period_previous_year(self):
        response = self.client.get(
            "/reports/income-statement?date_from=2026-11-01&date_to=2026-11-30&compare=previous_year"
        )
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("مقارنة مع 2025-11-01 — 2025-11-30", body)
        self.assertIn("800.00", body)  # Prior-year revenue
        self.assertIn("900.00", body)  # Current-period net revenue
        self.assertIn("470.00", body)  # Prior-year net profit

    def test_trial_balance_can_exclude_closing_entries(self):
        included = self.client.get(
            "/reports/trial-balance?date_from=2026-11-01&date_to=2026-11-30"
        )
        excluded = self.client.get(
            "/reports/trial-balance?date_from=2026-11-01&date_to=2026-11-30&include_closing=0"
        )
        self.assertEqual(included.status_code, 200)
        self.assertEqual(excluded.status_code, 200)
        self.assertIn(">1000.00</td><td>1000.00</td>", included.get_data(as_text=True))
        self.assertNotIn(">1000.00</td><td>1000.00</td>", excluded.get_data(as_text=True))

    def test_comparison_period_supports_previous_period_and_leap_year(self):
        self.assertEqual(
            comparison_period("2026-03-01", "2026-03-31", "previous_period"),
            ("2026-01-29", "2026-02-28"),
        )
        self.assertEqual(
            comparison_period("2024-02-01", "2024-02-29", "previous_year"),
            ("2023-02-01", "2023-02-28"),
        )

    def test_invalid_or_blank_dates_restore_year_to_date_defaults(self):
        today = date(2026, 11, 12)
        expected = ("2026-01-01", "2026-11-12")
        self.assertEqual(normalize_income_statement_period("not-a-date", "2026-11-12", today), expected)
        self.assertEqual(normalize_income_statement_period("", "", today), expected)
        self.assertEqual(normalize_income_statement_period("2026-12-01", "2026-01-01", today), expected)

    def test_income_statement_filters_revenue_and_direct_cost_by_same_dimensions(self):
        response = self.client.get("/reports/income-statement?date_from=2026-11-01&date_to=2026-11-30&compare=none&project_id=1")
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("مشروع ألف", body)
        self.assertIn("1,000.00", body)
        self.assertIn("200.00", body)
        self.assertIn("800.00", body)
        self.assertNotIn("900.00", body)

    def test_income_statement_exports_keep_selected_analytic_dimensions(self):
        from flask import Flask
        from io import BytesIO
        from openpyxl import load_workbook
        from report_exports import register_report_exports

        export_app = Flask(__name__)
        export_app.secret_key = "export-test-secret"
        register_report_exports(export_app, self.db_path, {"admin": {"reports.view"}})
        client = export_app.test_client()
        with client.session_transaction() as user_session:
            user_session["user_id"] = 1
            user_session["role"] = "admin"
            user_session["permissions"] = ["reports.view"]

        query = (
            "?date_from=2026-11-01&date_to=2026-11-30&compare=none"
            "&project_id=1"
        )
        excel = client.get("/reports/export/income-statement/excel" + query)
        self.assertEqual(excel.status_code, 200)
        workbook = load_workbook(BytesIO(excel.data), data_only=True)
        values = [cell.value for row in workbook.active.iter_rows() for cell in row]
        self.assertIn(1000, values)
        self.assertIn(200, values)
        self.assertIn(800, values)
        self.assertNotIn(900, values)

        pdf = client.get("/reports/export/income-statement/pdf" + query)
        if pdf.status_code == 503:
            self.skipTest("Arabic PDF font is not installed in this test environment.")
        self.assertEqual(pdf.status_code, 200)
        self.assertTrue(pdf.data.startswith(b"%PDF"))

    def test_shared_calculation_returns_expected_sections(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        statement = build_income_statement(conn, "2026-11-01", "2026-11-30")
        conn.close()
        self.assertEqual(statement["revenue_total"], 900)
        self.assertEqual(statement["cogs_total"], 200)
        self.assertEqual(statement["gross_profit"], 700)
        self.assertEqual(statement["operating_expense_total"], 250)
        self.assertEqual(statement["net_profit"], 450)


if __name__ == "__main__":
    unittest.main()
