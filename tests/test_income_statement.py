import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

import app as nexus_app
from accounting_core import register_decimal_aggregates
from income_statement import build_income_statement, normalize_income_statement_period


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
                (7,'2026-11-12','posted','year_end_closing');
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
                (11,7,5,'0','450');
            INSERT INTO company_settings VALUES('base_currency','EGP');
            INSERT INTO company_settings VALUES('company_name','شركة الاختبار');
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
            session["permissions"] = ["reports.view"]

    def tearDown(self):
        nexus_app.db = self.original_db
        self.temp.cleanup()

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

    def test_invalid_or_blank_dates_restore_year_to_date_defaults(self):
        today = date(2026, 11, 12)
        expected = ("2026-01-01", "2026-11-12")
        self.assertEqual(normalize_income_statement_period("not-a-date", "2026-11-12", today), expected)
        self.assertEqual(normalize_income_statement_period("", "", today), expected)
        self.assertEqual(normalize_income_statement_period("2026-12-01", "2026-01-01", today), expected)

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
