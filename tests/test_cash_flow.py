import sqlite3
import tempfile
import unittest
from pathlib import Path

from flask import Flask

import financial_planning
from accounting_core import register_decimal_aggregates


class CashFlowReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "cashflow.db"
        self.original_connect = financial_planning._connect

        def connect():
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            register_decimal_aggregates(conn)
            return conn

        financial_planning._connect = connect
        conn = connect()
        conn.executescript("""
          CREATE TABLE accounts(id INTEGER PRIMARY KEY,code TEXT,name_ar TEXT,account_type TEXT,is_group INTEGER DEFAULT 0);
          CREATE TABLE journal_entries(id INTEGER PRIMARY KEY,entry_date TEXT,status TEXT);
          CREATE TABLE journal_lines(id INTEGER PRIMARY KEY,entry_id INTEGER,account_id INTEGER,debit TEXT,credit TEXT);
          CREATE TABLE management_account_roles(role TEXT,account_id INTEGER);
          CREATE TABLE management_dashboard_meta(meta_key TEXT PRIMARY KEY,meta_value TEXT);
          INSERT INTO accounts VALUES
            (1,'1110','الصندوق','asset',0),
            (2,'3100','رأس المال','equity',0),
            (3,'4100','إيرادات المبيعات','revenue',0),
            (4,'6100','مصروفات تشغيلية','expense',0),
            (5,'1240','معدات','asset',0),
            (6,'2200','قرض طويل الأجل','liability',0);
          INSERT INTO management_account_roles VALUES('cash',1);
          INSERT INTO journal_entries VALUES
            (1,'2026-10-31','posted'),(2,'2026-11-05','posted'),
            (3,'2026-11-08','posted'),(4,'2026-11-12','posted'),
            (5,'2026-11-15','posted');
          INSERT INTO journal_lines VALUES
            (1,1,1,'1000','0'),(2,1,2,'0','1000'),
            (3,2,1,'300','0'),(4,2,3,'0','300'),
            (5,3,4,'80','0'),(6,3,1,'0','80'),
            (7,4,5,'200','0'),(8,4,1,'0','200'),
            (9,5,1,'500','0'),(10,5,6,'0','500');
        """)
        conn.commit()
        conn.close()

        templates = Path(__file__).resolve().parents[1] / "templates"
        self.app = Flask(__name__, template_folder=str(templates))
        self.app.secret_key = "test"
        self.app.jinja_env.globals["csrf_token"] = lambda: "test-token"
        self.app.jinja_env.filters["money"] = lambda value: f"{float(value or 0):,.2f}"
        self.app.context_processor(lambda: {
            "permissions": {"cashflow.view"},
            "license_info": {"status": "demo"},
        })
        self.app.add_url_rule("/change-password", endpoint="change_password", view_func=lambda: "")
        self.app.add_url_rule("/logout", endpoint="logout", view_func=lambda: "")
        self.app.register_blueprint(financial_planning.planning_bp)
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session["user_id"] = 1
            session["permissions"] = ["cashflow.view"]

    def tearDown(self):
        financial_planning._connect = self.original_connect
        self.temp.cleanup()

    def test_cash_flow_reconciles_opening_movements_and_closing_cash(self):
        response = self.client.get(
            "/reports/cash-flow?date_from=2026-11-01&date_to=2026-11-30"
        )
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("1,000.00", body)
        self.assertIn("800.00", body)
        self.assertIn("280.00", body)
        self.assertIn("1,520.00", body)
        self.assertIn("الأنشطة التشغيلية", body)
        self.assertIn("الأنشطة الاستثمارية", body)
        self.assertIn("الأنشطة التمويلية", body)


if __name__ == "__main__":
    unittest.main()
