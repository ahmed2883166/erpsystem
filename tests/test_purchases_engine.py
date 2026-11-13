import sqlite3
import tempfile
import unittest
from pathlib import Path
from flask import Flask
from unittest.mock import patch

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

    def test_purchase_order_requires_independent_approval_before_receipt(self):
        purchases.init_purchase_schema()
        self.db.execute("INSERT INTO suppliers(code,name) VALUES('SUP-APP','مورد اعتماد')")
        supplier_id = self.db.execute("SELECT id FROM suppliers WHERE code='SUP-APP'").fetchone()["id"]
        self.db.execute("INSERT INTO stock_products(id,sku,name_ar) VALUES(1,'PO-ITEM-1','صنف اختبار')")
        self.db.execute("""INSERT INTO purchase_orders(po_no,supplier_id,order_date,created_by)
                           VALUES('PO-APP-1',?,'2026-11-13',1)""", (supplier_id,))
        order_id = self.db.execute("SELECT id FROM purchase_orders WHERE po_no='PO-APP-1'").fetchone()["id"]
        self.db.execute("INSERT INTO purchase_order_lines(po_id,product_id,quantity,unit_price) VALUES(?,?,?,?)",
                        (order_id, 1, "3", "25"))
        self.db.commit()

        purchases.submit_purchase_order(self.db, order_id, 1)
        self.db.commit()
        state = self.db.execute("SELECT status,submitted_by FROM purchase_orders WHERE id=?", (order_id,)).fetchone()
        self.assertEqual(state["status"], "pending_approval")
        self.assertEqual(state["submitted_by"], 1)

        with self.assertRaisesRegex(ValueError, "لا يجوز اعتماد"):
            purchases.approve_purchase_order(self.db, order_id, 1)
        self.db.rollback()
        self.assertEqual(self.db.execute("SELECT status FROM purchase_orders WHERE id=?", (order_id,)).fetchone()[0],
                         "pending_approval")

        purchases.approve_purchase_order(self.db, order_id, 2, "مطابقة السعر والكمية")
        self.db.commit()
        approved = self.db.execute(
            "SELECT status,approved_by,approved_at,approval_reason FROM purchase_orders WHERE id=?", (order_id,)
        ).fetchone()
        self.assertEqual(approved["status"], "approved")
        self.assertEqual(approved["approved_by"], 2)
        self.assertTrue(approved["approved_at"])
        self.assertEqual(approved["approval_reason"], "مطابقة السعر والكمية")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM stock_movements").fetchone()[0], 0)

        with self.assertRaisesRegex(ValueError, "ليس بانتظار الاعتماد"):
            purchases.approve_purchase_order(self.db, order_id, 3)
        self.db.rollback()

    def test_purchase_order_rejection_requires_reason_and_can_be_resubmitted(self):
        purchases.init_purchase_schema()
        self.db.execute("INSERT INTO suppliers(code,name) VALUES('SUP-REJ','مورد رفض')")
        supplier_id = self.db.execute("SELECT id FROM suppliers WHERE code='SUP-REJ'").fetchone()["id"]
        self.db.execute("INSERT INTO stock_products(id,sku,name_ar) VALUES(1,'PO-ITEM-2','صنف اختبار')")
        self.db.execute("""INSERT INTO purchase_orders(po_no,supplier_id,order_date,created_by)
                           VALUES('PO-REJ-1',?,'2026-11-13',1)""", (supplier_id,))
        order_id = self.db.execute("SELECT id FROM purchase_orders WHERE po_no='PO-REJ-1'").fetchone()["id"]
        self.db.execute("INSERT INTO purchase_order_lines(po_id,product_id,quantity,unit_price) VALUES(?,?,?,?)",
                        (order_id, 1, "2", "40"))
        self.db.commit()
        purchases.submit_purchase_order(self.db, order_id, 1)
        self.db.commit()
        with self.assertRaisesRegex(ValueError, "سبب رفض"):
            purchases.reject_purchase_order(self.db, order_id, 2, "لا")
        self.db.rollback()
        purchases.reject_purchase_order(self.db, order_id, 2, "السعر أعلى من العرض")
        self.db.commit()
        rejected = self.db.execute("SELECT status,rejection_reason FROM purchase_orders WHERE id=?", (order_id,)).fetchone()
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(rejected["rejection_reason"], "السعر أعلى من العرض")
        purchases.submit_purchase_order(self.db, order_id, 1)
        self.db.commit()
        resubmitted = self.db.execute(
            "SELECT status,rejection_reason,approved_by FROM purchase_orders WHERE id=?", (order_id,)
        ).fetchone()
        self.assertEqual(resubmitted["status"], "pending_approval")
        self.assertIsNone(resubmitted["rejection_reason"])
        self.assertIsNone(resubmitted["approved_by"])

    def test_supplier_bill_requires_independent_approval_before_posting(self):
        purchases.init_purchase_schema()
        self.db.execute("INSERT INTO suppliers(code,name) VALUES('SUP-BILL-APP','مورد فاتورة')")
        supplier_id = self.db.execute("SELECT id FROM suppliers WHERE code='SUP-BILL-APP'").fetchone()["id"]
        self.db.execute("INSERT INTO stock_warehouses(code,name_ar) VALUES('WB-APP','مخزن فاتورة')")
        warehouse_id = self.db.execute("SELECT id FROM stock_warehouses WHERE code='WB-APP'").fetchone()["id"]
        self.db.execute("""INSERT INTO goods_receipts(receipt_no,supplier_id,warehouse_id,receipt_date,total_value)
            VALUES('GR-BILL-APP',? ,?,'2026-11-13','100')""", (supplier_id, warehouse_id))
        receipt_id = self.db.execute("SELECT id FROM goods_receipts WHERE receipt_no='GR-BILL-APP'").fetchone()["id"]
        self.db.execute("""INSERT INTO supplier_bills(bill_no,supplier_id,bill_date,due_date,supplier_invoice_no,
            subtotal,tax_rate,tax_amount,total_amount,created_by,status,bill_kind)
            VALUES('BILL-APP',?,'2026-11-13','2026-12-13','INV-APP','100','0','0','100',10,'draft','inventory')""",
            (supplier_id,))
        bill_id = self.db.execute("SELECT id FROM supplier_bills WHERE bill_no='BILL-APP'").fetchone()["id"]
        self.db.execute("INSERT INTO supplier_bill_receipts(bill_id,receipt_id,matched_amount) VALUES(?,?,?)",
                        (bill_id, receipt_id, "100"))
        self.db.commit()

        purchases.submit_supplier_bill(self.db, bill_id, 10)
        self.db.commit()
        self.assertEqual(self.db.execute("SELECT status FROM supplier_bills WHERE id=?", (bill_id,)).fetchone()[0],
                         "pending_approval")
        with self.assertRaisesRegex(ValueError, "لا يجوز اعتماد"):
            purchases.approve_supplier_bill(self.db, bill_id, 10)
        self.db.rollback()
        purchases.approve_supplier_bill(self.db, bill_id, 20, "مطابقة فاتورة المورد")
        self.db.commit()
        self.assertEqual(self.db.execute("SELECT status FROM supplier_bills WHERE id=?", (bill_id,)).fetchone()[0],
                         "approved")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0], 0)

        with patch("purchases.supplier_ap_account", return_value=99) as _ap_patch, patch("purchases.create_journal", return_value=77) as _journal_patch:
            entry_id = purchases.post_supplier_bill(self.db, bill_id, 30)
        self.db.commit()
        posted = self.db.execute("SELECT status,journal_entry_id FROM supplier_bills WHERE id=?", (bill_id,)).fetchone()
        self.assertEqual(posted["status"], "posted")
        self.assertEqual(posted["journal_entry_id"], entry_id)
        self.assertEqual(entry_id, 77)
        with self.assertRaisesRegex(ValueError, "قبل اعتمادها"):
            purchases.post_supplier_bill(self.db, bill_id, 30)
        self.db.rollback()

    def test_supplier_bill_rejection_requires_reason_and_can_be_resubmitted(self):
        purchases.init_purchase_schema()
        self.db.execute("INSERT INTO suppliers(code,name) VALUES('SUP-BILL-REJ','مورد رفض فاتورة')")
        supplier_id = self.db.execute("SELECT id FROM suppliers WHERE code='SUP-BILL-REJ'").fetchone()["id"]
        self.db.execute("""INSERT INTO supplier_bills(bill_no,supplier_id,bill_date,due_date,supplier_invoice_no,
            subtotal,tax_rate,tax_amount,total_amount,created_by,status,bill_kind,expense_account_id)
            VALUES('BILL-REJ',?,'2026-11-13','2026-12-13','INV-REJ','50','0','0','50',10,'draft','expense',?)""",
            (supplier_id, self.db.execute("SELECT id FROM accounts WHERE code='5000'").fetchone()["id"]))
        bill_id = self.db.execute("SELECT id FROM supplier_bills WHERE bill_no='BILL-REJ'").fetchone()["id"]
        self.db.commit()
        purchases.submit_supplier_bill(self.db, bill_id, 10)
        self.db.commit()
        with self.assertRaisesRegex(ValueError, "سبب رفض"):
            purchases.reject_supplier_bill(self.db, bill_id, 20, "لا")
        self.db.rollback()
        purchases.reject_supplier_bill(self.db, bill_id, 20, "المستند يحتاج مراجعة")
        self.db.commit()
        state = self.db.execute("SELECT status,rejection_reason FROM supplier_bills WHERE id=?", (bill_id,)).fetchone()
        self.assertEqual(state["status"], "rejected")
        self.assertEqual(state["rejection_reason"], "المستند يحتاج مراجعة")
        purchases.submit_supplier_bill(self.db, bill_id, 10)
        self.db.commit()
        state = self.db.execute("SELECT status,rejection_reason,approved_by FROM supplier_bills WHERE id=?", (bill_id,)).fetchone()
        self.assertEqual(state["status"], "pending_approval")
        self.assertIsNone(state["rejection_reason"])
        self.assertIsNone(state["approved_by"])

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

    def test_integrity_resolve_button_only_closes_after_duplicate_is_removed(self):
        self.db.executescript("""
          CREATE TABLE purchase_integrity_issues(
            id INTEGER PRIMARY KEY, issue_type TEXT, issue_key TEXT,
            detected_at TEXT DEFAULT CURRENT_TIMESTAMP, resolved INTEGER DEFAULT 0,
            resolved_by INTEGER, resolved_at TEXT
          );
          CREATE TABLE supplier_bills(
            id INTEGER PRIMARY KEY, supplier_id INTEGER, supplier_invoice_no TEXT
          );
          INSERT INTO purchase_integrity_issues(id,issue_type,issue_key)
            VALUES(1,'duplicate_supplier_invoice','1:inv-100');
          INSERT INTO supplier_bills VALUES(1,1,'INV-100');
          INSERT INTO supplier_bills VALUES(2,1,' inv-100 ');
        """)
        app = Flask(__name__)
        app.add_url_rule("/__test_global_search", endpoint="global_search", view_func=lambda: "")
        app.secret_key = "test"
        app.register_blueprint(purchases.purchase_bp)
        client = app.test_client()
        with client.session_transaction() as session:
            session["user_id"] = 1
            session["role"] = "admin"
        response = client.post("/purchases/integrity/1/resolve")
        self.assertEqual(response.status_code, 302)
        state = self.db.execute("SELECT resolved FROM purchase_integrity_issues WHERE id=1").fetchone()[0]
        self.assertEqual(state, 0)
        self.db.execute("DELETE FROM supplier_bills WHERE id=2")
        response = client.post("/purchases/integrity/1/resolve")
        self.assertEqual(response.status_code, 302)
        issue = self.db.execute(
            "SELECT resolved,resolved_by,resolved_at FROM purchase_integrity_issues WHERE id=1"
        ).fetchone()
        self.assertEqual(issue["resolved"], 1)
        self.assertEqual(issue["resolved_by"], 1)
        self.assertTrue(issue["resolved_at"])

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
