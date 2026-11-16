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
        CREATE TABLE supplier_bills(id INTEGER PRIMARY KEY,total_amount TEXT,paid_amount TEXT,status TEXT,journal_entry_id INTEGER,credited_amount TEXT DEFAULT '0');
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
        INSERT INTO supplier_bills VALUES(1,'100','30','posted',11,'0');
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

    def test_industrial_reconciliation_uses_only_1130_inventory_control(self):
        snapshot = reconciliation_snapshot(self.db)
        self.assertEqual(snapshot["stock_value"], 80.0)
        # Industrial v1 intentionally recognizes 1130 only; legacy 1150 balances are not silently reclassified.
        self.assertEqual(snapshot["gl_stock"], 50.0)
        self.assertEqual(snapshot["stock_difference"], -30.0)
        self.assertEqual(len(snapshot["unbalanced"]), 0)
        self.assertEqual(snapshot["inventory_account_codes"], ["1130"])

    def test_custom_activity_profile_controls_inventory_and_grni_reconciliation(self):
        self.db.executescript("""
            CREATE TABLE activity_profiles(
                id INTEGER PRIMARY KEY, code TEXT, name TEXT, base_activity TEXT,
                description TEXT, account_roles_json TEXT, posting_accounts_json TEXT,
                tax_roles_json TEXT, is_builtin INTEGER, active INTEGER
            );
            INSERT INTO accounts VALUES(11,'2191','التزامات استلام مخصصة','liability',NULL,0,1);
            INSERT INTO company_settings VALUES('activity_profile_code','custom_trade');
            INSERT INTO activity_profiles VALUES(
                1,'custom_trade','نشاط تجاري مخصص','commercial','',
                '{"inventory":["1140"],"grni":["2191"]}','{}','{}',0,1
            );
            INSERT INTO journal_entries VALUES
                (18,'2026-11-12','CUSTOM-STOCK','مخزون ملف النشاط','posted',1,'now'),
                (19,'2026-11-12','CUSTOM-GRNI','التزام ملف النشاط','posted',1,'now');
            INSERT INTO journal_lines VALUES
                (7,18,6,'80','0','مخزون حسب الملف المخصص'),
                (8,18,2,'0','80','مقابل المخزون'),
                (9,19,2,'60','0','مقابل الالتزام'),
                (10,19,11,'0','60','GRNI حسب الملف المخصص');
        """)
        snapshot = reconciliation_snapshot(self.db)
        self.assertEqual(snapshot["inventory_account_codes"], ["1140"])
        self.assertEqual(snapshot["gl_stock"], Decimal("80.00"))
        self.assertEqual(snapshot["stock_difference"], Decimal("0.00"))
        self.assertEqual(snapshot["grni_ledger"], Decimal("60.00"))
        self.assertEqual(snapshot["grni_difference"], Decimal("0.00"))

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

    def test_draft_supplier_bill_does_not_reduce_grni(self):
        self.db.execute("UPDATE supplier_bills SET status='draft' WHERE id=1")
        snapshot = reconciliation_snapshot(self.db)
        self.assertEqual(snapshot["grni"], 100.0)

    def test_grni_report_traces_open_receipt_to_remaining_and_consumed_stock(self):
        self.db.executescript("""
            ALTER TABLE goods_receipts ADD COLUMN receipt_no TEXT;
            ALTER TABLE goods_receipts ADD COLUMN receipt_date TEXT;
            ALTER TABLE goods_receipts ADD COLUMN supplier_id INTEGER;
            UPDATE goods_receipts SET receipt_no='GRN-TRACE-1',receipt_date='2026-11-10',supplier_id=1;
            CREATE TABLE suppliers(id INTEGER PRIMARY KEY,name TEXT);
            INSERT INTO suppliers VALUES(1,'مورد اختبار');
            ALTER TABLE stock_movements ADD COLUMN movement_no TEXT;
            INSERT INTO stock_movements(id,accounting_status,movement_no)
                VALUES(99,'posted','PUR-GRN-TRACE-1');
            ALTER TABLE stock_valuation_layers ADD COLUMN source_movement_id INTEGER;
            ALTER TABLE stock_valuation_layers ADD COLUMN value_remaining TEXT;
            UPDATE stock_valuation_layers SET source_movement_id=99,value_remaining='40' WHERE id=1;
            UPDATE stock_valuation_layers SET qty_remaining='0',value_remaining='0' WHERE id=2;
            INSERT INTO journal_entries VALUES(16,'2026-11-11','GRNI-TRACE-CREDIT','GRNI test','posted',1,'now');
            INSERT INTO journal_lines VALUES(7,16,9,'0','100','GRNI credit');
            INSERT INTO journal_entries VALUES(17,'2026-11-11','GRNI-TRACE-DEBIT','GRNI settlement','posted',1,'now');
            INSERT INTO journal_lines VALUES(8,17,9,'60','0','GRNI debit');
        """)
        snapshot = reconciliation_snapshot(self.db)
        self.assertEqual(snapshot["grni"], 60.0)
        self.assertEqual(snapshot["grni_ledger"], Decimal("40.00"))
        self.assertEqual(snapshot["grni_difference"], Decimal("-20.00"))
        self.assertEqual(snapshot["grni_receipts"][0]["receipt_no"], "GRN-TRACE-1")
        self.assertEqual(snapshot["grni_receipts"][0]["supplier_name"], "مورد اختبار")
        self.assertEqual(snapshot["grni_receipts"][0]["remaining_stock_value"], Decimal("40.00"))
        self.assertEqual(snapshot["grni_receipts"][0]["consumed_value"], Decimal("60.00"))
        self.assertEqual(snapshot["grni_consumed_value"], Decimal("60.00"))
        self.assertEqual(snapshot["grni_stock_gap"], Decimal("20.00"))

    def test_supplier_credit_notes_reduce_open_ap(self):
        self.db.execute("UPDATE supplier_bills SET credited_amount='10' WHERE id=1")
        snapshot = reconciliation_snapshot(self.db)
        self.assertEqual(snapshot["open_ap"], 60.0)

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



    def test_reconciliation_snapshot_excludes_copied_opening_from_inventory_gl(self):
        conn=sqlite3.connect(":memory:")
        conn.row_factory=sqlite3.Row
        conn.executescript("""
            CREATE TABLE accounts(id INTEGER PRIMARY KEY,code TEXT,name_ar TEXT,account_type TEXT,is_group INTEGER,active INTEGER);
            CREATE TABLE company_settings(setting_key TEXT PRIMARY KEY,setting_value TEXT);
            CREATE TABLE journal_entries(id INTEGER PRIMARY KEY,entry_date TEXT,reference TEXT,status TEXT);
            CREATE TABLE journal_lines(id INTEGER PRIMARY KEY,entry_id INTEGER,account_id INTEGER,debit TEXT,credit TEXT);
            CREATE TABLE stock_valuation_layers(id INTEGER PRIMARY KEY,qty_remaining TEXT,unit_cost TEXT);
            CREATE TABLE financial_years(id INTEGER PRIMARY KEY,year_name TEXT,start_date TEXT,end_date TEXT,status TEXT,opening_entry_id INTEGER);
            CREATE TABLE financial_year_journal_history(id INTEGER PRIMARY KEY,financial_year_id INTEGER,entry_kind TEXT,action TEXT,entry_id INTEGER,related_entry_id INTEGER);
            INSERT INTO accounts VALUES(1,'1130','مخزون خامات','asset',0,1);
            INSERT INTO company_settings VALUES('activity','industrial');
            INSERT INTO stock_valuation_layers VALUES(1,'10','10');
            INSERT INTO journal_entries VALUES(1,'2026-10-31','OLD','posted'),(2,'2026-11-01','OPEN','posted');
            INSERT INTO journal_lines VALUES(1,1,1,'100','0'),(2,2,1,'100','0');
            INSERT INTO financial_years VALUES(1,'2026','2026-11-01','2027-10-31','open',2);
            INSERT INTO financial_year_journal_history(financial_year_id,entry_kind,action,entry_id)
              VALUES(1,'opening','created',2);
        """)
        snapshot=reconciliation_snapshot(conn)
        conn.close()
        self.assertEqual(snapshot["gl_stock"],Decimal("100.00"))
        self.assertEqual(snapshot["stock_difference"],Decimal("0.00"))

if __name__ == "__main__":
    unittest.main()
