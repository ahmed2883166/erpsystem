import sqlite3
import tempfile
import unittest
from pathlib import Path

import purchases
import stock


class PurchasingAccountingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db_path = Path(self.tmp.name) / "nexus.db"
        purchases.DB_PATH = db_path
        stock.DB_PATH = db_path
        stock.initialize_stock_schema()
        self.db = purchases.db()
        self.db.executescript("""
        CREATE TABLE accounts(
          id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL, name_ar TEXT NOT NULL,
          account_type TEXT NOT NULL, parent_id INTEGER, is_group INTEGER NOT NULL DEFAULT 0,
          active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE journal_entries(
          id INTEGER PRIMARY KEY, entry_date TEXT NOT NULL, reference TEXT, description TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'posted', created_by INTEGER, created_at TEXT NOT NULL
        );
        CREATE TABLE journal_lines(
          id INTEGER PRIMARY KEY, entry_id INTEGER NOT NULL, account_id INTEGER NOT NULL,
          debit TEXT NOT NULL DEFAULT '0', credit TEXT NOT NULL DEFAULT '0', memo TEXT
        );
        CREATE TABLE company_settings(setting_key TEXT PRIMARY KEY, setting_value TEXT NOT NULL);
        """)
        self.db.execute("INSERT INTO accounts(code,name_ar,account_type,is_group) VALUES('2000','الالتزامات','liability',1)")
        self.db.execute("INSERT INTO accounts(code,name_ar,account_type,is_group) VALUES('2190','GRNI','liability',0)")
        self.db.execute("INSERT INTO accounts(code,name_ar,account_type,is_group) VALUES('1100','الأصول المتداولة','asset',1)")
        self.db.execute("INSERT INTO accounts(code,name_ar,account_type,is_group) VALUES('1110','البنك','asset',0)")
        self.db.execute("INSERT INTO accounts(code,name_ar,account_type,is_group) VALUES('5000','مصروف','expense',0)")
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_purchase_schema_is_created(self):
        purchases.init_purchase_schema()
        names = {r["name"] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("suppliers", names)
        self.assertIn("supplier_bills", names)
        self.assertIn("supplier_payments", names)
        self.assertIn("supplier_payment_allocations", names)

    def test_supplier_invoice_number_and_receipt_can_only_be_used_once(self):
        purchases.init_purchase_schema()
        self.db.execute("INSERT INTO suppliers(code,name) VALUES('SUP-1','مورد اختبار')")
        supplier_id = self.db.execute("SELECT id FROM suppliers WHERE code='SUP-1'").fetchone()["id"]
        self.db.execute("INSERT INTO stock_warehouses(code,name_ar) VALUES('W1','مخزن')")
        warehouse_id = self.db.execute("SELECT id FROM stock_warehouses WHERE code='W1'").fetchone()["id"]
        self.db.execute("""INSERT INTO goods_receipts(receipt_no,supplier_id,warehouse_id,receipt_date,total_value)
                           VALUES('GR-1',?,?, '2026-11-10','100')""", (supplier_id, warehouse_id))
        receipt_id = self.db.execute("SELECT id FROM goods_receipts WHERE receipt_no='GR-1'").fetchone()["id"]
        self.db.execute("""INSERT INTO supplier_bills(bill_no,supplier_id,bill_date,supplier_invoice_no,
                           subtotal,tax_rate,tax_amount,total_amount)
                           VALUES('BILL-1',?,'2026-11-10','DUP','100','0','0','100')""", (supplier_id,))
        bill_id = self.db.execute("SELECT id FROM supplier_bills WHERE bill_no='BILL-1'").fetchone()["id"]
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("""INSERT INTO supplier_bills(bill_no,supplier_id,bill_date,supplier_invoice_no,
                               subtotal,tax_rate,tax_amount,total_amount)
                               VALUES('BILL-2',?,'2026-11-10',' dup ','100','0','0','100')""", (supplier_id,))
        self.db.execute("INSERT INTO supplier_bill_receipts(bill_id,receipt_id,matched_amount) VALUES(?,?,?)",
                        (bill_id, receipt_id, "100"))
        self.db.execute("""INSERT INTO supplier_bills(bill_no,supplier_id,bill_date,supplier_invoice_no,
                           subtotal,tax_rate,tax_amount,total_amount)
                           VALUES('BILL-3',?,'2026-11-10','UNIQUE-2','100','0','0','100')""", (supplier_id,))
        other_bill = self.db.execute("SELECT id FROM supplier_bills WHERE bill_no='BILL-3'").fetchone()["id"]
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("INSERT INTO supplier_bill_receipts(bill_id,receipt_id,matched_amount) VALUES(?,?,?)",
                            (other_bill, receipt_id, "100"))

    def test_journal_posting_is_balanced(self):
        dr = purchases.account(self.db, "5000", "expense")
        cr = purchases.account(self.db, "2190", "liability")
        entry_id = purchases.create_journal(
            self.db, "2026-11-09", "TEST-1", "اختبار قيد",
            [(dr, purchases.money("125.50"), 0, "مدين"),
             (cr, 0, purchases.money("125.50"), "دائن")], 1)
        from decimal import Decimal
        rows = self.db.execute(
            "SELECT debit,credit FROM journal_lines WHERE entry_id=?", (entry_id,)
        ).fetchall()
        debit = sum((Decimal(row["debit"]) for row in rows), Decimal("0"))
        credit = sum((Decimal(row["credit"]) for row in rows), Decimal("0"))
        self.assertEqual(debit, credit)

    def test_group_account_cannot_be_posted(self):
        with self.assertRaises(ValueError):
            purchases.account(self.db, "2000", "liability")

    def test_detail_account_setup_rejects_automatic_account_creation(self):
        before = self.db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
        with self.assertRaisesRegex(ValueError, "غير مضبوط"):
            purchases.ensure_detail_account(
                self.db, "2191", "GRNI فرعي", "liability", "2000")
        after = self.db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
