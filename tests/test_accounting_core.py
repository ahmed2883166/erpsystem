import sqlite3
from decimal import Decimal
import unittest
from accounting_core import post_journal, reconciliation_snapshot, register_decimal_aggregates


class SharedAccountingCoreTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        CREATE TABLE accounts(id INTEGER PRIMARY KEY,code TEXT,name_ar TEXT,account_type TEXT,parent_id INTEGER,is_group INTEGER,active INTEGER);
        CREATE TABLE journal_entries(id INTEGER PRIMARY KEY,entry_date TEXT,reference TEXT,description TEXT,status TEXT,created_by INTEGER,created_at TEXT);
        CREATE TABLE journal_lines(id INTEGER PRIMARY KEY,entry_id INTEGER,account_id INTEGER,debit TEXT,credit TEXT,memo TEXT);
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
        from decimal import Decimal
        lines = self.db.execute("SELECT debit,credit FROM journal_lines WHERE entry_id=?", (eid,)).fetchall()
        debit = sum((Decimal(row["debit"]) for row in lines), Decimal("0"))
        credit = sum((Decimal(row["credit"]) for row in lines), Decimal("0"))
        self.assertEqual(debit, credit)
        self.assertEqual(debit, Decimal("100.00"))

    def test_same_reference_is_idempotent(self):
        one = post_journal(self.db, "2026-11-09", "INV-2", "بيع",
                           [(1, 50, 0, "نقدية"), (2, 0, 50, "إيراد")], 7)
        two = post_journal(self.db, "2026-11-09", "INV-2", "بيع",
                           [(1, 50, 0, "نقدية"), (2, 0, 50, "إيراد")], 7)
        self.assertEqual(one, two)
        self.assertEqual(self.db.execute("SELECT COUNT(*) n FROM journal_entries WHERE reference='INV-2'").fetchone()["n"], 1)

    def test_reused_reference_with_different_payload_is_rejected(self):
        post_journal(self.db, "2026-11-09", "INV-DUP", "بيع",
                     [(1, 50, 0, "نقدية"), (2, 0, 50, "إيراد")], 7)
        with self.assertRaisesRegex(ValueError, "مرجع القيد مستخدم بالفعل"):
            post_journal(self.db, "2026-11-09", "INV-DUP", "فاتورة أخرى",
                         [(1, 75, 0, "نقدية"), (2, 0, 75, "إيراد")], 7)

    def test_unbalanced_journal_rejected(self):
        with self.assertRaisesRegex(ValueError, "غير متوازن"):
            post_journal(self.db, "2026-11-09", "INV-3", "خطأ",
                         [(1, 100, 0, "مدين"), (2, 0, 90, "دائن")], 7)

    def test_unknown_analytical_dimension_is_rejected_instead_of_silently_ignored(self):
        with self.assertRaisesRegex(ValueError, "أبعاد تحليلية غير معروفة"):
            post_journal(
                self.db, "2026-11-09", "INV-UNKNOWN-DIM", "اختبار بُعد غير معروف",
                [(1, 25, 0, "مدين"), (2, 0, 25, "دائن")], 7,
                {"brnch_id": 1},
            )
        self.assertEqual(
            self.db.execute(
                "SELECT COUNT(*) FROM journal_entries WHERE reference='INV-UNKNOWN-DIM'"
            ).fetchone()[0],
            0,
        )

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

    def test_sql_decimal_aggregates_preserve_text_precision(self):
        register_decimal_aggregates(self.db)
        self.db.execute("CREATE TABLE decimal_values(debit TEXT,credit TEXT)")
        self.db.executemany("INSERT INTO decimal_values VALUES(?,?)", [
            ("0.10","0"), ("0.20","0"), ("0","0.30")
        ])
        totals = self.db.execute(
            "SELECT DECIMAL_SUM(debit),DECIMAL_SUM(credit),DECIMAL_NET(debit,credit) FROM decimal_values"
        ).fetchone()
        self.assertEqual(totals[0], "0.30")
        self.assertEqual(totals[1], "0.30")
        self.assertEqual(totals[2], "0.00")

    def test_reconciliation_sums_text_money_exactly(self):
        self.db.execute(
            "INSERT INTO journal_entries VALUES(16,'2026-11-11','DECIMAL-1','دقة عشرية','posted',1,'now')"
        )
        self.db.execute("INSERT INTO journal_lines VALUES(7,16,1,'0.10','0','مدين')")
        self.db.execute("INSERT INTO journal_lines VALUES(8,16,1,'0.20','0','مدين')")
        self.db.execute("INSERT INTO journal_lines VALUES(9,16,2,'0','0.30','دائن')")
        snapshot = reconciliation_snapshot(self.db)
        self.assertEqual(snapshot["journal_totals"]["debit"], Decimal("120.30"))
        self.assertEqual(snapshot["journal_totals"]["credit"], Decimal("120.30"))
        self.assertFalse(any(row["reference"] == "DECIMAL-1" for row in snapshot["unbalanced"]))

    def test_reconciliation_uses_one_grouped_journal_query_for_large_ledger(self):
        for entry_id in range(100, 400):
            self.db.execute(
                "INSERT INTO journal_entries VALUES(?,?,?,'large-ledger','posted',1,'now')",
                (entry_id, "2026-11-11", f"BULK-{entry_id}"),
            )
            self.db.execute(
                "INSERT INTO journal_lines VALUES(?,?,1,'0.01','0','bulk debit')",
                (entry_id * 2, entry_id),
            )
            self.db.execute(
                "INSERT INTO journal_lines VALUES(?,?,2,'0','0.01','bulk credit')",
                (entry_id * 2 + 1, entry_id),
            )
        statements = []
        self.db.set_trace_callback(statements.append)
        snapshot = reconciliation_snapshot(self.db)
        self.db.set_trace_callback(None)

        self.assertEqual(snapshot["journal_totals"]["debit"], Decimal("123.00"))
        self.assertEqual(snapshot["journal_totals"]["credit"], Decimal("123.00"))
        self.assertEqual(len(snapshot["unbalanced"]), 0)
        grouped_queries = [
            sql for sql in statements
            if "LEFT JOIN journal_lines jl ON jl.entry_id=je.id" in sql
        ]
        per_entry_queries = [
            sql for sql in statements
            if "SELECT debit,credit FROM journal_lines WHERE entry_id=" in sql
        ]
        self.assertEqual(len(grouped_queries), 1)
        self.assertEqual(per_entry_queries, [])

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
