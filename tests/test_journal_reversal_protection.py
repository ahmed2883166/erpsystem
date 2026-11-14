import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as nexus_app
from database import connect_database


class JournalReversalProtectionTests(unittest.TestCase):
    def setUp(self):
        nexus_app.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "reversals.db"
        conn = connect_database(self.db_path)
        conn.executescript("""
            CREATE TABLE users(id INTEGER PRIMARY KEY, username TEXT, role TEXT);
            CREATE TABLE accounts(
                id INTEGER PRIMARY KEY, code TEXT, name_ar TEXT, account_type TEXT,
                is_group INTEGER DEFAULT 0, active INTEGER DEFAULT 1
            );
            CREATE TABLE journal_entries(
                id INTEGER PRIMARY KEY, entry_date TEXT, reference TEXT, description TEXT,
                status TEXT, created_by INTEGER, created_at TEXT
            );
            CREATE TABLE journal_lines(
                id INTEGER PRIMARY KEY, entry_id INTEGER, account_id INTEGER,
                debit TEXT DEFAULT '0', credit TEXT DEFAULT '0', memo TEXT DEFAULT ''
            );
            CREATE TABLE financial_years(
                id INTEGER PRIMARY KEY, year_name TEXT, opening_entry_id INTEGER,
                closing_entry_id INTEGER, status TEXT, start_date TEXT, end_date TEXT
            );
            INSERT INTO accounts(id,code,name_ar,account_type) VALUES
                (1,'1110','الصندوق','asset'),(2,'4100','الإيرادات','revenue');
            INSERT INTO journal_entries VALUES
                (1,'2026-01-01','JRN-1','قيد أصلي','posted',1,'2026-01-01'),
                (2,'2026-01-02','REV-1','قيد عكسي للقيد رقم 1','posted',1,'2026-01-02'),
                (3,'2026-01-03','JRN-3','قيد تم عكسه','posted',1,'2026-01-03'),
                (4,'2026-01-04','REV-3','قيد عكسي للقيد رقم 3','posted',1,'2026-01-04');
            INSERT INTO journal_lines VALUES
                (1,1,1,'100.00','0','أصل'),
                (2,1,2,'0','100.00','مقابل'),
                (3,2,1,'0','100.00','عكس الأصل'),
                (4,2,2,'100.00','0','عكس المقابل'),
                (5,3,1,'100.00','0','أصل'),
                (6,3,2,'0','100.00','مقابل'),
                (7,4,1,'0','100.00','عكس الأصل'),
                (8,4,2,'100.00','0','عكس المقابل');
        """)
        conn.commit()
        conn.close()
        self.patches = [
            patch.object(nexus_app, "db", side_effect=lambda: connect_database(self.db_path)),
            patch.object(nexus_app, "linked_operational_source", return_value=None),
            patch.object(nexus_app, "log_change", return_value=None),
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

    def test_reversal_of_reversal_restores_original_balances(self):
        response = self.client.post(
            "/journal/2/reverse",
            data={"reversal_date": "2026-01-05", "reversal_reason": "إلغاء القيد العكسي الخاطئ"},
        )
        self.assertEqual(response.status_code, 302)
        with connect_database(self.db_path) as conn:
            row = conn.execute(
                "SELECT id FROM journal_entries WHERE reference='REV-2'"
            ).fetchone()
            self.assertIsNotNone(row)
            lines = conn.execute(
                "SELECT account_id,debit,credit FROM journal_lines WHERE entry_id=? ORDER BY id",
                (row["id"],),
            ).fetchall()
            self.assertEqual(
                [(x["account_id"], x["debit"], x["credit"]) for x in lines],
                [(1, "100.00", "0"), (2, "0", "100.00")],
            )
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0], 5)

    def test_original_journal_cannot_be_reversed_twice(self):
        response = self.client.post(
            "/journal/1/reverse",
            data={"reversal_date": "2026-01-05", "reversal_reason": "محاولة عكس مكرر"},
        )
        self.assertEqual(response.status_code, 302)
        with connect_database(self.db_path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0]
        self.assertEqual(count, 4)

    def test_journal_detail_links_original_and_reversal(self):
        original = self.client.get("/journal/1")
        reversal = self.client.get("/journal/2")
        self.assertEqual(original.status_code, 200)
        self.assertEqual(reversal.status_code, 200)
        self.assertIn('href="/journal/2"', original.get_data(as_text=True))
        self.assertIn('href="/journal/1"', reversal.get_data(as_text=True))
        self.assertIn("إنشاء قيد عكسي", reversal.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
