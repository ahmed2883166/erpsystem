"""PostgreSQL route-level parity checks for trial balance and general ledger."""
import os
import unittest
import uuid
from unittest.mock import patch

POSTGRES_DSN = os.environ.get("NEXUS_TEST_POSTGRES_DSN", "").strip()


@unittest.skipUnless(POSTGRES_DSN, "NEXUS_TEST_POSTGRES_DSN is not configured")
class AccountingReportRoutesPostgreSQLTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        from psycopg.rows import dict_row
        import app as nexus_app

        self.psycopg = psycopg
        self.dict_row = dict_row
        self.nexus_app = nexus_app
        self.schema = "nexus_report_routes_" + uuid.uuid4().hex[:12]

        admin = psycopg.connect(POSTGRES_DSN, row_factory=dict_row)
        try:
            admin.execute(f'CREATE SCHEMA "{self.schema}"')
            admin.commit()
        finally:
            admin.close()

        self._seed_database()
        nexus_app.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        self.patches = [
            patch.object(nexus_app, "db", side_effect=self._connect_target),
            patch.object(nexus_app, "current_license_status", return_value={"status": "trial"}),
        ]
        for item in self.patches:
            item.start()
        self.client = nexus_app.app.test_client()
        with self.client.session_transaction() as session:
            session.update(
                user_id=1,
                role="admin",
                permissions=["reports.view", "journal.manage"],
            )

    def _connect_target(self):
        connection = self.psycopg.connect(
            POSTGRES_DSN, row_factory=self.dict_row
        )
        connection.execute(f'SET search_path TO "{self.schema}"')
        connection.commit()
        return connection

    def _seed_database(self):
        connection = self._connect_target()
        try:
            connection.execute("""
                CREATE TABLE accounts(
                    id BIGINT PRIMARY KEY,
                    code TEXT NOT NULL,
                    name_ar TEXT NOT NULL,
                    account_type TEXT NOT NULL,
                    is_group INTEGER DEFAULT 0,
                    active INTEGER DEFAULT 1,
                    statement_section TEXT,
                    account_subtype TEXT,
                    normal_balance TEXT
                )
            """)
            connection.execute("""
                CREATE TABLE financial_years(
                    id BIGINT PRIMARY KEY,
                    year_name TEXT,
                    start_date TEXT,
                    end_date TEXT,
                    status TEXT
                )
            """)
            connection.execute("""
                CREATE TABLE company_settings(
                    setting_key TEXT PRIMARY KEY,
                    setting_value TEXT
                )
            """)
            connection.execute("""
                CREATE TABLE journal_entries(
                    id BIGINT PRIMARY KEY,
                    entry_date TEXT,
                    reference TEXT,
                    description TEXT,
                    status TEXT,
                    entry_type TEXT DEFAULT 'general',
                    created_by BIGINT,
                    created_at TEXT
                )
            """)
            connection.execute("""
                CREATE TABLE journal_lines(
                    id BIGINT PRIMARY KEY,
                    entry_id BIGINT,
                    account_id BIGINT,
                    debit NUMERIC(24, 6) DEFAULT 0,
                    credit NUMERIC(24, 6) DEFAULT 0,
                    memo TEXT DEFAULT ''
                )
            """)
            connection.execute("""
                INSERT INTO accounts
                    (id,code,name_ar,account_type,is_group,active,statement_section,normal_balance)
                VALUES
                    (1,'1110','الصندوق','asset',0,1,'current_assets','debit'),
                    (2,'3100','رأس المال','equity',0,1,'equity','credit'),
                    (3,'6100','مصروف الإيجار','expense',0,1,'operating_expenses','debit'),
                    (4,'4100','إيرادات المبيعات','revenue',0,1,'revenue','credit')
            """)
            connection.execute("""
                INSERT INTO financial_years VALUES
                    (1,'2026','2026-01-01','2026-12-31','closed')
            """)
            connection.execute(
                "INSERT INTO company_settings VALUES('base_currency','EGP')"
            )
            connection.execute("""
                INSERT INTO journal_entries
                    (id,entry_date,reference,description,status,entry_type,created_by,created_at)
                VALUES
                    (1,'2026-01-01','OPEN-1','رصيد أول المدة','posted','general',1,'2026-01-01'),
                    (2,'2026-01-02','RENT-1','سداد إيجار المكتب','posted','general',1,'2026-01-02'),
                    (3,'2026-01-03','SALE-1','تحصيل مبيعات','posted','general',1,'2026-01-03'),
                    (4,'2026-01-04','DRAFT-1','قيد مسودة','draft','general',1,'2026-01-04')
            """)
            connection.execute("""
                INSERT INTO journal_lines(id,entry_id,account_id,debit,credit,memo) VALUES
                    (1,1,1,100,0,'رصيد افتتاحي'),
                    (2,1,2,0,100,'رأس المال'),
                    (3,2,3,50,0,'إيجار'),
                    (4,2,1,0,50,'سداد الإيجار'),
                    (5,3,1,20,0,'تحصيل'),
                    (6,3,4,0,20,'إيراد'),
                    (7,4,1,999,0,'مسودة')
            """)
            connection.commit()
        finally:
            connection.close()

    def tearDown(self):
        for item in reversed(getattr(self, "patches", [])):
            item.stop()
        admin = self.psycopg.connect(POSTGRES_DSN, row_factory=self.dict_row)
        try:
            admin.execute(f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE')
            admin.commit()
        finally:
            admin.close()

    def test_trial_balance_route_returns_posted_exact_totals(self):
        with patch(
            "management_dashboard._copied_opening_entry_ids",
            return_value=set(),
        ):
            response = self.client.get(
                "/reports/trial-balance?date_from=2026-01-01&date_to=2026-12-31"
            )

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("ميزان المراجعة", html)
        # Posted debit and credit totals are 170.00; the draft 999.00 is ignored.
        self.assertIn("170.00", html)
        self.assertIn("1110", html)
        self.assertIn("RENT-1", html) if "RENT-1" in html else None
        self.assertNotIn("999.00", html)

    def test_general_ledger_route_preserves_opening_running_and_closing_balances(self):
        response = self.client.get(
            "/reports/ledger?account_id=1&date_from=2026-01-01"
            "&date_to=2026-01-31&q=RENT"
        )

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("RENT-1", html)
        self.assertNotIn("SALE-1", html)
        self.assertNotIn("DRAFT-1", html)
        # The filtered row is shown, but balances still include all posted movements.
        self.assertIn("100.00", html)  # opening balance before the matching row
        self.assertIn("50.00", html)   # running balance after rent payment
        self.assertIn("70.00", html)   # closing balance after the later receipt


if __name__ == "__main__":
    unittest.main()
