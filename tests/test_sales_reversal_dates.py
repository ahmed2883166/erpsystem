import sqlite3
import unittest

from sales import _reverse_posted_journal


class SalesReversalDateTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("""
            CREATE TABLE journal_entries(
                id INTEGER PRIMARY KEY, status TEXT NOT NULL, entry_date TEXT NOT NULL
            )
        """)
        self.conn.execute(
            "INSERT INTO journal_entries(id,status,entry_date) VALUES(1,'posted','2026-05-12')"
        )

    def tearDown(self):
        self.conn.close()

    def test_reversal_cannot_precede_original_journal_date(self):
        with self.assertRaisesRegex(ValueError, "يسبق تاريخ القيد الأصلي"):
            _reverse_posted_journal(
                self.conn, 1, "2026-05-11", "REV-1", "عكس اختبار", 1
            )

    def test_invalid_reversal_date_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "تاريخ القيد العكسي غير صحيح"):
            _reverse_posted_journal(
                self.conn, 1, "not-a-date", "REV-1", "عكس اختبار", 1
            )


if __name__ == "__main__":
    unittest.main()
