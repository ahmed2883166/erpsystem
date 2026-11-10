import sqlite3
import unittest

from purchases import ensure_detail_account


class AccountMappingPolicyTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("""CREATE TABLE accounts(
            id INTEGER PRIMARY KEY, code TEXT, name_ar TEXT, account_type TEXT,
            is_group INTEGER DEFAULT 0, active INTEGER DEFAULT 1
        )""")

    def tearDown(self):
        self.conn.close()

    def test_missing_detail_account_is_rejected_not_created(self):
        with self.assertRaisesRegex(ValueError, "غير مضبوط"):
            ensure_detail_account(
                self.conn, "2190", "GRNI", "liability", "2100"
            )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0], 0
        )

    def test_existing_valid_detail_account_is_returned(self):
        self.conn.execute(
            "INSERT INTO accounts(code,name_ar,account_type,is_group,active) VALUES(?,?,?,?,?)",
            ("2190", "GRNI", "liability", 0, 1),
        )
        account_id = ensure_detail_account(
            self.conn, "2190", "GRNI", "liability", "2100"
        )
        self.assertEqual(account_id, 1)


if __name__ == "__main__":
    unittest.main()
