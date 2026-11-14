import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as nexus_app
from database import connect_database


class JournalAndLedgerFilterTests(unittest.TestCase):
    def setUp(self):
        nexus_app.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "filters.db"
        conn = connect_database(self.db_path)
        conn.executescript("""
            CREATE TABLE accounts(
                id INTEGER PRIMARY KEY,code TEXT,name_ar TEXT,account_type TEXT,
                is_group INTEGER DEFAULT 0,active INTEGER DEFAULT 1
            );
            CREATE TABLE journal_entries(
                id INTEGER PRIMARY KEY,entry_date TEXT,reference TEXT,description TEXT,
                status TEXT,entry_type TEXT DEFAULT 'general',created_by INTEGER,created_at TEXT
            );
            CREATE TABLE journal_lines(
                id INTEGER PRIMARY KEY,entry_id INTEGER,account_id INTEGER,
                debit TEXT DEFAULT '0',credit TEXT DEFAULT '0',memo TEXT DEFAULT ''
            );
            CREATE TABLE financial_years(
                id INTEGER PRIMARY KEY,year_name TEXT,start_date TEXT,end_date TEXT,status TEXT
            );
            CREATE TABLE company_settings(setting_key TEXT PRIMARY KEY,setting_value TEXT);
            INSERT INTO financial_years VALUES(1,'2026','2026-01-01','2026-12-31','open');
            INSERT INTO company_settings VALUES('base_currency','EGP');
            INSERT INTO accounts(id,code,name_ar,account_type) VALUES
                (1,'1110','الصندوق','asset'),(2,'4100','الإيرادات','revenue');
            INSERT INTO journal_entries VALUES
                (1,'2026-01-01','OPEN-1','Opening cash','posted','general',1,'2026-01-01'),
                (2,'2026-01-02','INV-RENT','Monthly rent','posted','general',1,'2026-01-02'),
                (3,'2026-01-03','RCPT-3','Customer payment','posted','general',1,'2026-01-03'),
                (4,'2026-01-04','DRAFT-4','Draft item','draft','general',1,'2026-01-04');
            INSERT INTO journal_lines VALUES
                (1,1,1,'100.00','0','Opening'),
                (2,1,2,'0','100.00','Opening'),
                (3,2,1,'50.00','0','Office rent'),
                (4,2,2,'0','50.00','Rent expense'),
                (5,3,1,'0','20.00','Receipt'),
                (6,3,2,'20.00','0','Receipt'),
                (7,4,1,'999.00','0','Draft');
        """)
        conn.commit()
        conn.close()
        self.patches = [
            patch.object(nexus_app, "db", side_effect=lambda: connect_database(self.db_path)),
            patch.object(nexus_app, "active_dimension_options", return_value={
                "branches": [], "cost_centers": [], "projects": []
            }),
            patch.object(nexus_app, "current_license_status", return_value={"status": "trial"}),
        ]
        for item in self.patches:
            item.start()
        self.client = nexus_app.app.test_client()
        with self.client.session_transaction() as session:
            session.update(user_id=1, role="admin", permissions=["journal.manage", "reports.view"])

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def test_journal_search_and_status_date_filters(self):
        response = self.client.get(
            "/journal?q=INV&status=posted&date_from=2026-01-01&date_to=2026-01-03"
        )
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("INV-RENT", html)
        self.assertNotIn("RCPT-3", html)
        self.assertNotIn("DRAFT-4", html)
        self.assertIn("إجمالي القيود: 1", html)
        self.assertIn('href="/journal/2"', html)
        self.assertIn("عرض القيد", html)

    def test_journal_detail_displays_the_posted_entry_lines(self):
        response = self.client.get("/journal/2")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Monthly rent", html)
        self.assertIn("1110", html)
        self.assertIn("Office rent", html)
        self.assertIn("50.00", html)

    def test_journal_search_includes_line_memos_and_account_filter(self):
        memo_response = self.client.get("/journal?q=Office+rent&status=posted")
        self.assertEqual(memo_response.status_code, 200)
        self.assertIn("INV-RENT", memo_response.get_data(as_text=True))

        account_response = self.client.get("/journal?account_id=2&status=posted")
        self.assertEqual(account_response.status_code, 200)
        account_html = account_response.get_data(as_text=True)
        self.assertIn("INV-RENT", account_html)
        self.assertIn("RCPT-3", account_html)
        self.assertNotIn("DRAFT-4", account_html)

    def test_ledger_search_preserves_full_running_and_closing_balances(self):
        response = self.client.get(
            "/reports/ledger?account_id=1&date_from=2026-01-01&q=rent"
        )
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("INV-RENT", html)
        self.assertNotIn("RCPT-3", html)
        self.assertIn("100.00", html)  # Balance before the first matching movement.
        self.assertIn("150.00", html)  # Running balance includes the opening movement.
        self.assertIn("130.00", html)  # Closing balance includes later, non-matching movements.

    def test_ledger_empty_search_does_not_zero_the_real_closing_balance(self):
        response = self.client.get(
            "/reports/ledger?account_id=1&date_from=2026-01-01&q=does-not-exist"
        )
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("لا توجد حركة خلال الفترة", html)
        self.assertIn("130.00", html)

    def test_trial_balance_route_aggregates_posted_entries_and_excludes_drafts(self):
        response = self.client.get(
            "/reports/trial-balance?date_from=2026-01-01&date_to=2026-01-31"
        )
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("1110", html)
        self.assertIn("4100", html)
        self.assertIn("170.00", html)  # Posted debit and credit totals only.
        self.assertIn("130.00", html)  # Cash net debit after the posted movements.
        self.assertIn("-130.00", html)  # Revenue account net credit.
        self.assertNotIn("999.00", html)  # Draft journal line must not enter the report.


if __name__ == "__main__":
    unittest.main()
