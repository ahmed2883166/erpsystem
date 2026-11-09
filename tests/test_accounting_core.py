import sqlite3
import unittest
from accounting_core import post_journal


class SharedAccountingCoreTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(":memory:")
        self.db.row_factory=sqlite3.Row
        self.db.executescript("""
        CREATE TABLE accounts(id INTEGER PRIMARY KEY,code TEXT,name_ar TEXT,account_type TEXT,parent_id INTEGER,is_group INTEGER,active INTEGER);
        CREATE TABLE journal_entries(id INTEGER PRIMARY KEY,entry_date TEXT,reference TEXT,description TEXT,status TEXT,created_by INTEGER,created_at TEXT);
        CREATE TABLE journal_lines(id INTEGER PRIMARY KEY,entry_id INTEGER,account_id INTEGER,debit REAL,credit REAL,memo TEXT);
        INSERT INTO accounts VALUES(1,'1110','الصندوق','asset',NULL,0,1);
        INSERT INTO accounts VALUES(2,'4100','الإيراد','revenue',NULL,0,1);
        INSERT INTO accounts VALUES(3,'1000','مجموعة الأصول','asset',NULL,1,1);
        INSERT INTO accounts VALUES(4,'1120','حساب موقوف','asset',NULL,0,0);
        """)

    def tearDown(self):
        self.db.close()

    def test_balanced_journal_posts(self):
        eid=post_journal(self.db,"2026-11-09","INV-1","بيع",[(1,100,0,"نقدية"),(2,0,100,"إيراد")],7)
        totals=self.db.execute("SELECT SUM(debit) d,SUM(credit) c FROM journal_lines WHERE entry_id=?",(eid,)).fetchone()
        self.assertEqual(totals["d"],totals["c"])
        self.assertEqual(totals["d"],100)

    def test_same_reference_is_idempotent(self):
        one=post_journal(self.db,"2026-11-09","INV-2","بيع",[(1,50,0,"نقدية"),(2,0,50,"إيراد")],7)
        two=post_journal(self.db,"2026-11-09","INV-2","تكرار",[(1,50,0,"نقدية"),(2,0,50,"إيراد")],7)
        self.assertEqual(one,two)
        self.assertEqual(self.db.execute("SELECT COUNT(*) n FROM journal_entries").fetchone()["n"],1)

    def test_unbalanced_journal_rejected(self):
        with self.assertRaisesRegex(ValueError,"غير متوازن"):
            post_journal(self.db,"2026-11-09","INV-3","خطأ",[(1,100,0,"مدين"),(2,0,90,"دائن")],7)

    def test_group_and_inactive_accounts_rejected(self):
        with self.assertRaises(ValueError):
            post_journal(self.db,"2026-11-09","INV-4","خطأ",[(3,100,0,"مدين"),(2,0,100,"دائن")],7)
        with self.assertRaises(ValueError):
            post_journal(self.db,"2026-11-09","INV-5","خطأ",[(4,100,0,"مدين"),(2,0,100,"دائن")],7)


if __name__=="__main__":
    unittest.main()
