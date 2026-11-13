import sqlite3
import unittest
from unittest.mock import patch

import app as nexus_app


class KeepOpenConnection(sqlite3.Connection):
    def close(self):
        # The route closes its connection; keep the in-memory test DB inspectable.
        pass


class AccountChartSafetyTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:", factory=KeepOpenConnection)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE accounts(
                id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL,
                name_ar TEXT NOT NULL, account_type TEXT NOT NULL,
                parent_id INTEGER, is_group INTEGER NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE journal_lines(
                id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL
            );
            INSERT INTO accounts(id,code,name_ar,account_type,is_group,active)
                VALUES(1,'1110','الصندوق','asset',0,1),
                      (2,'9999','حساب مستخدم','asset',0,1);
            INSERT INTO journal_lines(account_id) VALUES(2);
        """)
        self.client = nexus_app.app.test_client()

    def tearDown(self):
        self.conn.close()

    def _edit(self, account_id, new_code):
        with self.client.session_transaction() as session:
            session["user_id"] = 1
            session["permissions"] = ["accounts.manage"]
        with patch("app.db", return_value=self.conn):
            return self.client.post(
                f"/accounts/{account_id}/edit",
                data={
                    "code": new_code,
                    "name_ar": "اسم معدل",
                    "account_type": "asset",
                    "parent_id": "",
                },
                follow_redirects=True,
            )

    def test_operational_account_code_cannot_change_without_journal_lines(self):
        self._edit(1, "1111")
        row = self.conn.execute(
            "SELECT code FROM accounts WHERE id=1"
        ).fetchone()
        self.assertEqual(row["code"], "1110")

    def test_account_code_used_in_posted_history_cannot_change(self):
        self._edit(2, "9998")
        row = self.conn.execute(
            "SELECT code FROM accounts WHERE id=2"
        ).fetchone()
        self.assertEqual(row["code"], "9999")


if __name__ == "__main__":
    unittest.main()
