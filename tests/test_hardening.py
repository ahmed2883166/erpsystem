import sqlite3
import unittest

from accounting_core import post_journal


class JournalHardeningTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE accounts(
                id INTEGER PRIMARY KEY, code TEXT, name_ar TEXT, account_type TEXT,
                parent_id INTEGER, is_group INTEGER DEFAULT 0, active INTEGER DEFAULT 1
            );
            CREATE TABLE journal_entries(
                id INTEGER PRIMARY KEY, entry_date TEXT, reference TEXT, description TEXT,
                status TEXT, created_by INTEGER, created_at TEXT
            );
            CREATE TABLE journal_lines(
                id INTEGER PRIMARY KEY, entry_id INTEGER, account_id INTEGER,
                debit REAL, credit REAL, memo TEXT
            );
            CREATE TABLE accounting_periods(
                id INTEGER PRIMARY KEY, period_name TEXT, start_date TEXT, end_date TEXT,
                locked INTEGER DEFAULT 0, created_at TEXT
            );
            INSERT INTO accounts VALUES(1,'1110','Cash','asset',NULL,0,1);
            INSERT INTO accounts VALUES(2,'4100','Revenue','revenue',NULL,0,1);
        """)

    def tearDown(self):
        self.db.close()

    def test_invalid_date_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "تاريخ القيد"):
            post_journal(self.db, "2026-02-31", "BAD-DATE", "Invalid date",
                         [(1, 10, 0, ""), (2, 0, 10, "")])

    def test_closed_period_rejects_posting(self):
        self.db.execute(
            "INSERT INTO accounting_periods(period_name,start_date,end_date,locked,created_at) VALUES(?,?,?,?,?)",
            ("November 2026", "2026-11-01", "2026-11-30", 1, "now"),
        )
        with self.assertRaisesRegex(ValueError, "الفترة المحاسبية مغلقة"):
            post_journal(self.db, "2026-11-11", "LOCKED", "Locked period",
                         [(1, 10, 0, ""), (2, 0, 10, "")])

    def test_non_finite_amount_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "محدودة"):
            post_journal(self.db, "2026-11-11", "NAN", "NaN amount",
                         [(1, "NaN", 0, ""), (2, 0, 10, "")])

    def test_balanced_entry_in_open_period_is_allowed(self):
        entry_id = post_journal(self.db, "2026-11-11", "OPEN", "Open period",
                                [(1, 10, 0, ""), (2, 0, 10, "")])
        self.assertGreater(entry_id, 0)


if __name__ == "__main__":
    unittest.main()
