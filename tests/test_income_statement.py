import sqlite3
import tempfile
import unittest
from pathlib import Path

import app as nexus_app
from accounting_core import register_decimal_aggregates


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
            CREATE TABLE journal_entries(id INTEGER PRIMARY KEY, entry_date TEXT, status TEXT);
            CREATE TABLE journal_lines(
                id INTEGER PRIMARY KEY, entry_id INTEGER, account_id INTEGER,
                debit TEXT, credit TEXT
            );
            CREATE TABLE company_settings(setting_key TEXT PRIMARY KEY, setting_value TEXT);
            INSERT INTO accounts VALUES
                (1,'4100','إيرادات المبيعات','revenue',0,1),
                (2,'4110','مردودات المبيعات','revenue',0,1),
                (3,'6100','مصروفات التشغيل','expense',0,1);
            INSERT INTO journal_entries VALUES
                (1,'2026-11-10','posted'),(2,'2026-11-11','posted'),
                (3,'2026-11-12','posted'),(4,'2026-11-12','posted'),
                (5,'2026-11-12','draft');
            INSERT INTO journal_lines VALUES
                (1,1,1,'0','1000'),(2,2,2,'100','0'),
                (3,3,3,'300','0'),(4,4,3,'0','50'),
                (5,5,1,'0','500');
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

    def test_income_statement_calculates_natural_balances_and_excludes_drafts(self):
        response = self.client.get(
            "/reports/income-statement?date_from=2026-11-01&date_to=2026-11-30"
        )
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("900.00", body)
        self.assertIn("250.00", body)
        self.assertIn("650.00", body)
        self.assertIn("شركة الاختبار", body)


if __name__ == "__main__":
    unittest.main()
