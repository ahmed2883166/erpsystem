import sqlite3
import unittest
from accounting_core import post_journal, reconciliation_snapshot


class SharedAccountingCoreTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        CREATE TABLE accounts(id INTEGER PRIMARY KEY,code TEXT,name_ar TEXT,account_type TEXT,parent_id INTEGER,is_group INTEGER,active INTEGER);
        CREATE TABLE journal_entries(id INTEGER PRIMARY KEY,entry_date TEXT,reference TEXT,description TEXT,status TEXT,created_by INTEGER,created_at TEXT);
        CREATE TABLE journal_lines(id INTEGER PRIMARY KEY,entry_id INTEGER,account_id INTEGER,debit REAL,credit REAL,memo TEXT);
        CREATE TABLE company_settings(setting_key TEXT PRIMARY KEY,setting_value TEXT);
        CREATE TABLE stock_valuation_layers(id INTEGER PRIMARY KEY,qty_remaining TEXT,unit_cost TEXT);
        CREATE TABLE goods_receipts(id INTEGER PRIMARY KEY,total_value TEXT,status TEXT,journal_entry_id INTEGER);
        CREATE TABLE supplier_bill_receipts(bill_id INTEGER,receipt_id INTEGER,matched_amount TEXT);
        CREATE TABLE supplier_bills(id INTEGER PRIMARY KEY,total_amount TEXT,paid_amount TEXT,status TEXT,journal_entry_id INTEGER);
        CREATE TABLE supplier_payments(id INTEGER PRIMARY KEY,journal_entry_id INTEGER);
        CREATE TABLE sales_invoices(id INTEGER PRIMARY KEY,journal_entry_id INTEGER);
        CREATE TABLE customer_receipts(id INTEGER PRIMARY KEY,journal_entry_id INTEGER);
        CREATE TABLE bank_transactions(id INTEGER PRIMARY KEY,journal_entry_id INTEGER);
        CREATE TABLE stock_movements(id INTEGER PRIMARY KEY,accounting_status TEXT);
        INSERT INTO accounts VALUES(1,'1110','الصندوق','asset',NULL,0,1);
        INSERT INTO accounts VALUES(2,'4100','الإيراد','revenue',NULL,0,1);
        INSERT INTO accounts VALUES(3,'1000','مجموعة الأصول','asset',NULL,1,1);
        INSERT INTO accounts VALUES(4,'1120','حساب موقوف','asset',NULL,0,0);
        INSERT INTO accounts VALUES(5,'1130','مخزون خامات','asset',NULL,0,1);
        INSERT INTO accounts VALUES(6,'1140','تحت التشغيل','asset',NULL,0,1);
        INSERT INTO accounts VALUES(7,'1150','إنتاج تام','asset',NULL,0,1);
        INSERT INTO accounts VALUES(8,'1160','قطع غيار','asset',NULL,0,1);
        INSERT INTO accounts VALUES(9,'2190','GRNI','liability',NULL,0,1);
        INSERT INTO accounts VALUES(10,'2110','الموردون','liability',NULL,0,1);
        INSERT INTO company_settings VALUES('activity','industrial');
        INSERT INTO stock_valuation_layers VALUES(1,'2','10');
        INSERT INTO stock_valuation_layers VALUES(2,'3','20');
        INSERT INTO goods_receipts VALUES(1,'100','posted',10);
        INSERT INTO supplier_bill_receipts VALUES(1,1,'40');
        INSERT INTO supplier_bills VALUES(1,'100','30','posted',11);
        INSERT INTO supplier_payments VALUES(1,12);
        INSERT INTO sales_invoices VALUES(1,13);
        INSERT INTO customer_receipts VALUES(1,14);
        INSERT INTO bank_transactions VALUES(1,15);
        INSERT INTO stock_movements VALUES(1,'pending_document');
        INSERT INTO journal_entries VALUES(10,'2026-11-10','GR-1','استلام','posted',1,'now');
        INSERT INTO journal_lines VALUES(1,10,5,50,0,'مخزون');
        INSERT INTO journal_lines VALUES(2,10,9,0,50,'GRNI');
        INSERT INTO journal_entries VALUES(11,'2026-11-10','BILL-1','فاتورة','posted',1,'now');
        INSERT INTO journal_lines VALUES(3,11,9,50,0,'تسوية GRNI');
        INSERT INTO journal_lines VALUES(4,11,10,0,50,'مورد');
        INSERT INTO journal_entries VALUES(12,'2026-11-10','PAY-1','سداد','posted',1,'now');
        INSERT INTO journal_lines VALUES(5,12,7,20,0,'مخزون إنتاج تام');
        INSERT INTO journal_lines VALUES(6,12,2,0,20,'حساب مقابل');
        INSERT INTO journal_entries VALUES(13,'2026-11-10','SALE-1','بيع','posted',1,'now');
        INSERT INTO journal_entries VALUES(14,'2026-11-10','REC-1','تحصيل','posted',1,'now');
        INSERT INTO journal_entries VALUES(15,'2026-11-10','BANK-1','بنك','posted',1,'now');
        """)

    def tearDown(self):
        self.db.close()

    def test_balanced_journal_posts(self):
        eid = post_journal(self.db, "2026-11-09", "INV-1", "بيع",
                           [(1, 100, 0, "نقدية"), (2, 0, 100, "إيراد")], 7)
        totals = self.db.execute(
            "SELECT SUM(debit) d,SUM(credit) c FROM journal_lines WHERE entry_id=?", (eid,)
        ).fetchone()
        self.assertEqual(totals["d"], totals["c"])
        self.assertEqual(totals["d"], 100)

    def test_same_reference_is_idempotent(self):
        one = post_journal(self.db, "2026-11-09", "INV-2", "بيع",
                           [(1, 50, 0, "نقدية"), (2, 0, 50, "إيراد")], 7)
        two = post_journal(self.db, "2026-11-09", "INV-2", "تكرار",
                           [(1, 50, 0, "نقدية"), (2, 0, 50, "إيراد")], 7)
        self.assertEqual(one, two)
        self.assertEqual(self.db.execute("SELECT COUNT(*) n FROM journal_entries WHERE reference='INV-2'").fetchone()["n"], 1)

    def test_unbalanced_journal_rejected(self):
        with self.assertRaisesRegex(ValueError, "غير متوازن"):
            post_journal(self.db, "2026-11-09", "INV-3", "خطأ",
                         [(1, 100, 0, "مدين"), (2, 0, 90, "دائن")], 7)

    def test_group_and_inactive_accounts_rejected(self):
        with self.assertRaises(ValueError):
            post_journal(self.db, "2026-11-09", "INV-4", "خطأ",
                         [(3, 100, 0, "مدين"), (2, 0, 100, "دائن")], 7)
        with self.assertRaises(ValueError):
            post_journal(self.db, "2026-11-09", "INV-5", "خطأ",
                         [(4, 100, 0, "مدين"), (2, 0, 100, "دائن")], 7)

    def test_industrial_reconciliation_includes_all_inventory_accounts(self):
        snapshot = reconciliation_snapshot(self.db)
        self.assertEqual(snapshot["stock_value"], 80.0)
        # GL = 50 debit on 1130 + 20 debit on 1150; other control accounts included as zero.
        self.assertEqual(snapshot["gl_stock"], 70.0)
        self.assertEqual(snapshot["stock_difference"], -10.0)
        self.assertEqual(len(snapshot["unbalanced"]), 0)
        self.assertEqual(snapshot["inventory_account_codes"], ["1130", "1140", "1150", "1160"])

    def test_partial_receipt_match_keeps_unmatched_grni(self):
        snapshot = reconciliation_snapshot(self.db)
        self.assertEqual(snapshot["grni"], 60.0)

    def test_unlinked_document_and_pending_stock_detection(self):
        snapshot = reconciliation_snapshot(self.db)
        self.assertEqual(snapshot["unlinked_documents"], [])
        self.assertEqual(snapshot["pending_stock"], 1)

    def test_linkage_report_flags_documents_without_journal(self):
        self.db.execute("INSERT INTO bank_transactions VALUES(2,NULL)")
        snapshot = reconciliation_snapshot(self.db)
        self.assertEqual(snapshot["unlinked_documents"], [{"label": "الحركات البنكية", "count": 1}])

    def test_snapshot_handles_optional_tables_missing(self):
        self.db.execute("DROP TABLE bank_transactions")
        self.db.execute("DROP TABLE customer_receipts")
        snapshot = reconciliation_snapshot(self.db)
        self.assertIn("grni", snapshot)
        self.assertIn("open_ap", snapshot)


if __name__ == "__main__":
    unittest.main()
