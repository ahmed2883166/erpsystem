import tempfile
import unittest
from decimal import Decimal
from unittest.mock import patch
from pathlib import Path

from flask import Flask
from flask_wtf.csrf import CSRFProtect

import purchases
import stock


class PurchaseExpenseBillRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        path = Path(self.tmp.name) / "nexus.db"
        purchases.DB_PATH = path
        stock.DB_PATH = path
        stock.initialize_stock_schema()
        self.conn = purchases.db()
        self.conn.executescript("""
        CREATE TABLE accounts(
            id INTEGER PRIMARY KEY, code TEXT UNIQUE, name_ar TEXT,
            account_type TEXT, parent_id INTEGER, is_group INTEGER DEFAULT 0,
            active INTEGER DEFAULT 1
        );
        CREATE TABLE journal_entries(
            id INTEGER PRIMARY KEY, entry_date TEXT, reference TEXT,
            description TEXT, status TEXT, created_by INTEGER, created_at TEXT
        );
        CREATE TABLE journal_lines(
            id INTEGER PRIMARY KEY, entry_id INTEGER, account_id INTEGER,
            debit TEXT DEFAULT '0', credit TEXT DEFAULT '0', memo TEXT
        );
        CREATE TABLE company_settings(
            setting_key TEXT PRIMARY KEY, setting_value TEXT NOT NULL
        );
        INSERT INTO company_settings VALUES('activity','commercial');
        INSERT INTO accounts(code,name_ar,account_type,is_group,active)
            VALUES('2110','الموردون','liability',0,1);
        INSERT INTO accounts(code,name_ar,account_type,is_group,active)
            VALUES('5000','مصروف إيجار','expense',0,1);
        INSERT INTO accounts(code,name_ar,account_type,is_group,active)
            VALUES('1165','ضريبة قيمة مضافة مدخلات','asset',0,1);
        INSERT INTO accounts(code,name_ar,account_type,is_group,active)
            VALUES('2190','بضاعة مستلمة غير مفوترة GRNI','liability',0,1);
        """)
        purchases.init_purchase_schema()
        self.conn.execute("INSERT INTO suppliers(code,name) VALUES('SUP-1','مورد اختبار')")
        self.supplier_id = self.conn.execute(
            "SELECT id FROM suppliers WHERE code='SUP-1'"
        ).fetchone()["id"]
        self.expense_id = self.conn.execute(
            "SELECT id FROM accounts WHERE code='5000'"
        ).fetchone()["id"]
        self.conn.commit()
        self.conn.close()

        self.app = Flask(__name__)
        self.app.add_url_rule("/__test_global_search", endpoint="global_search", view_func=lambda: "")
        self.app.secret_key = "test-secret"
        self.app.register_blueprint(purchases.purchase_bp)
        self.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        CSRFProtect(self.app)
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def _submit_approve_and_post(self, bill_id, creator_id=1):
        with self.client.session_transaction() as session:
            session["user_id"] = creator_id
            session["role"] = "accountant"
        response = self.client.post(f"/purchases/bills/{bill_id}/submit")
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as session:
            session["user_id"] = creator_id + 1
            session["role"] = "admin"
        response = self.client.post(f"/purchases/bills/{bill_id}/approve", data={"reason": "مطابقة الفاتورة"})
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as session:
            session["user_id"] = creator_id
            session["role"] = "accountant"
        response = self.client.post(f"/purchases/bills/{bill_id}/post")
        self.assertEqual(response.status_code, 302)


    def test_integrity_screen_displays_legacy_duplicate_supplier_invoices(self):
        conn = purchases.db()
        conn.execute("DROP INDEX IF EXISTS uq_supplier_bill_invoice_no")
        for bill_no in ("LEGACY-1", "LEGACY-2"):
            conn.execute("""INSERT INTO supplier_bills(
                bill_no,supplier_id,bill_date,due_date,supplier_invoice_no,
                subtotal,tax_rate,tax_amount,total_amount
            ) VALUES(?,?,?, ?,?,'100','0','0','100')""",
                (bill_no, self.supplier_id, "2026-11-10", "2026-11-30", "DUP-OLD"))
        conn.commit()
        conn.close()
        purchases.init_purchase_schema()
        with self.client.session_transaction() as session:
            session["user_id"] = 1
            session["role"] = "accountant"
        with patch("purchases.render_template", side_effect=lambda _template, **ctx:
                   " ".join(row["bill_no"] for item in ctx["issues"] for row in item["rows"])):
            response = self.client.get("/purchases/integrity")
        self.assertEqual(response.status_code, 200)
        self.assertIn("LEGACY-1", response.get_data(as_text=True))
        self.assertIn("LEGACY-2", response.get_data(as_text=True))

    def test_integrity_issue_cannot_be_closed_while_duplicate_remains(self):
        conn = purchases.db()
        conn.execute("DROP INDEX IF EXISTS uq_supplier_bill_invoice_no")
        for bill_no in ("DUP-A", "DUP-B"):
            conn.execute("""INSERT INTO supplier_bills(
                bill_no,supplier_id,bill_date,supplier_invoice_no,
                subtotal,tax_rate,tax_amount,total_amount
            ) VALUES(?,?,?,'DUP-RESOLVE','100','0','0','100')""",
                (bill_no, self.supplier_id, "2026-11-10"))
        conn.commit()
        conn.close()
        purchases.init_purchase_schema()
        with self.client.session_transaction() as session:
            session["user_id"] = 7
            session["role"] = "accountant"
        conn = purchases.db()
        issue_id = conn.execute(
            "SELECT id FROM purchase_integrity_issues WHERE issue_type='duplicate_supplier_invoice'"
        ).fetchone()["id"]
        conn.close()
        response = self.client.post(f"/purchases/integrity/{issue_id}/resolve")
        self.assertEqual(response.status_code, 302)
        conn = purchases.db()
        issue = conn.execute(
            "SELECT resolved FROM purchase_integrity_issues WHERE id=?", (issue_id,)
        ).fetchone()
        self.assertEqual(issue["resolved"], 0)
        conn.close()

    def test_integrity_issue_records_user_and_time_after_duplicate_is_fixed(self):
        conn = purchases.db()
        conn.execute("DROP INDEX IF EXISTS uq_supplier_bill_invoice_no")
        for bill_no in ("FIX-A", "FIX-B"):
            conn.execute("""INSERT INTO supplier_bills(
                bill_no,supplier_id,bill_date,supplier_invoice_no,
                subtotal,tax_rate,tax_amount,total_amount
            ) VALUES(?,?,?,'DUP-FIX','100','0','0','100')""",
                (bill_no, self.supplier_id, "2026-11-10"))
        conn.commit()
        conn.close()
        purchases.init_purchase_schema()
        with self.client.session_transaction() as session:
            session["user_id"] = 9
            session["role"] = "accountant"
        conn = purchases.db()
        issue_id = conn.execute(
            "SELECT id FROM purchase_integrity_issues WHERE issue_type='duplicate_supplier_invoice'"
        ).fetchone()["id"]
        conn.execute("DELETE FROM supplier_bills WHERE bill_no='FIX-B'")
        conn.commit()
        conn.close()
        response = self.client.post(f"/purchases/integrity/{issue_id}/resolve")
        self.assertEqual(response.status_code, 302)
        conn = purchases.db()
        issue = conn.execute(
            "SELECT resolved,resolved_by,resolved_at FROM purchase_integrity_issues WHERE id=?",
            (issue_id,),
        ).fetchone()
        self.assertEqual(issue["resolved"], 1)
        self.assertEqual(issue["resolved_by"], 9)
        self.assertTrue(issue["resolved_at"])
        conn.close()

    def test_custom_activity_profile_controls_supplier_bill_payable_account(self):
        import json
        from activity_profiles import (
            initialize_activity_profiles_schema, create_activity_profile,
            list_activity_profiles, update_custom_activity_profile,
        )
        from chart_templates import ensure_activity_chart_accounts
        conn = purchases.db()
        ensure_activity_chart_accounts(conn, "commercial")
        conn.execute(
            "INSERT INTO accounts(code,name_ar,account_type) VALUES('2111','موردون مخصصون','liability')"
        )
        initialize_activity_profiles_schema(conn)
        profile_id = create_activity_profile(
            conn, "custom_purchase", "ملف مشتريات مخصص", "commercial"
        )
        profile = next(p for p in list_activity_profiles(conn) if p["id"] == profile_id)
        roles = dict(profile["account_roles"])
        roles["payables"] = ["2111"]
        posting = dict(profile["posting_accounts"])
        posting["supplier_ap"] = "2111"
        update_custom_activity_profile(
            conn, profile_id, json.dumps(roles), json.dumps(posting),
            json.dumps(profile["tax_roles"]),
        )
        conn.execute(
            "INSERT INTO company_settings(setting_key,setting_value) VALUES('activity_profile_code','custom_purchase')"
        )
        conn.commit()
        conn.close()
        with self.client.session_transaction() as session:
            session["user_id"] = 1
            session["role"] = "accountant"
        response = self.client.post("/purchases/bills/new", data={
            "supplier_id": str(self.supplier_id),
            "supplier_invoice_no": "CUSTOM-AP-001",
            "bill_date": "2026-11-11",
            "due_date": "2026-11-30",
            "bill_kind": "expense",
            "expense_account_id": str(self.expense_id),
            "subtotal": "100.00",
            "tax_rate": "0",
        })
        self.assertEqual(response.status_code, 302)
        conn = purchases.db()
        bill = conn.execute(
            "SELECT id,status,journal_entry_id FROM supplier_bills WHERE supplier_invoice_no='CUSTOM-AP-001'"
        ).fetchone()
        self.assertEqual(bill["status"], "draft")
        self.assertIsNone(bill["journal_entry_id"])
        bill_id = bill["id"]
        conn.close()
        self._submit_approve_and_post(bill_id)
        conn = purchases.db()
        bill = conn.execute("SELECT journal_entry_id FROM supplier_bills WHERE id=?", (bill_id,)).fetchone()
        lines = conn.execute(
            """SELECT a.code,jl.debit,jl.credit FROM journal_lines jl
               JOIN accounts a ON a.id=jl.account_id WHERE jl.entry_id=? ORDER BY jl.id""",
            (bill["journal_entry_id"],),
        ).fetchall()
        self.assertEqual(
            [(row["code"], Decimal(row["debit"]), Decimal(row["credit"])) for row in lines],
            [("5000", Decimal("100.00"), Decimal("0.00")),
             ("2111", Decimal("0.00"), Decimal("100.00"))],
        )
        conn.close()

    def test_rejected_supplier_bill_can_be_edited_and_returns_to_draft(self):
        conn = purchases.db()
        conn.execute("""INSERT INTO supplier_bills(
            bill_no,supplier_id,bill_date,due_date,supplier_invoice_no,subtotal,tax_rate,
            tax_amount,total_amount,created_by,status,bill_kind,expense_account_id,
            submitted_by,submitted_at,rejected_by,rejected_at,rejection_reason
        ) VALUES('EDIT-REJ',?,'2026-11-11','2026-11-30','EDIT-INV','100','0','0','100',
            1,'rejected','expense',?,1,CURRENT_TIMESTAMP,2,CURRENT_TIMESTAMP,'مطلوب تصحيح القيمة')""",
            (self.supplier_id, self.expense_id))
        bill_id = conn.execute("SELECT id FROM supplier_bills WHERE bill_no='EDIT-REJ'").fetchone()["id"]
        conn.commit()
        conn.close()
        with self.client.session_transaction() as session:
            session["user_id"] = 1
            session["role"] = "accountant"

        from jinja2 import Environment
        template_source = (Path(__file__).resolve().parents[1] / "templates" / "purchase_bill_new.html").read_text(encoding="utf-8")
        Environment().parse(template_source)
        with patch("purchases.render_template", side_effect=lambda template, **context:
                   f"{template}:{context['bill']['supplier_invoice_no']}:{context['edit_mode']}"):
            response = self.client.get(f"/purchases/bills/{bill_id}/edit")
        self.assertEqual(response.status_code, 200)
        self.assertIn("purchase_bill_new.html:EDIT-INV:True", response.get_data(as_text=True))

        response = self.client.post(f"/purchases/bills/{bill_id}/edit", data={
            "supplier_id": str(self.supplier_id),
            "supplier_invoice_no": "EDIT-INV-UPDATED",
            "bill_date": "2026-11-12",
            "due_date": "2026-12-01",
            "bill_kind": "expense",
            "expense_account_id": str(self.expense_id),
            "subtotal": "125.00",
            "tax_rate": "14",
        })
        self.assertEqual(response.status_code, 302)
        conn = purchases.db()
        bill = conn.execute("SELECT * FROM supplier_bills WHERE id=?", (bill_id,)).fetchone()
        self.assertEqual(bill["status"], "draft")
        self.assertEqual(bill["supplier_invoice_no"], "EDIT-INV-UPDATED")
        self.assertEqual(bill["subtotal"], "125.00")
        self.assertEqual(bill["tax_amount"], "17.50")
        self.assertEqual(bill["total_amount"], "142.50")
        self.assertEqual(bill["bill_date"], "2026-11-12")
        self.assertEqual(bill["due_date"], "2026-12-01")
        self.assertIsNone(bill["journal_entry_id"])
        self.assertIsNone(bill["submitted_by"])
        self.assertIsNone(bill["approved_by"])
        self.assertIsNone(bill["rejected_by"])
        self.assertIsNone(bill["rejection_reason"])
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM journal_entries").fetchone()[0], 0)
        conn.close()

    def test_posted_supplier_bill_cannot_be_edited(self):
        conn = purchases.db()
        conn.execute("""INSERT INTO supplier_bills(
            bill_no,supplier_id,bill_date,supplier_invoice_no,subtotal,tax_rate,tax_amount,
            total_amount,created_by,status,bill_kind,expense_account_id,journal_entry_id
        ) VALUES('EDIT-POSTED',?,'2026-11-11','EDIT-POSTED-INV','100','0','0','100',
            1,'posted','expense',?,55)""", (self.supplier_id, self.expense_id))
        bill_id = conn.execute("SELECT id FROM supplier_bills WHERE bill_no='EDIT-POSTED'").fetchone()["id"]
        conn.commit()
        conn.close()
        with self.client.session_transaction() as session:
            session["user_id"] = 1
            session["role"] = "accountant"
        response = self.client.get(f"/purchases/bills/{bill_id}/edit")
        self.assertEqual(response.status_code, 302)
        conn = purchases.db()
        bill = conn.execute("SELECT status,journal_entry_id FROM supplier_bills WHERE id=?", (bill_id,)).fetchone()
        self.assertEqual(bill["status"], "posted")
        self.assertEqual(bill["journal_entry_id"], 55)
        conn.close()

    def _create_inventory_bill_with_receipts(self, bill_no, invoice_no, receipt_amounts, subtotal, tax_rate="0"):
        conn = purchases.db()
        warehouse = conn.execute(
            "INSERT INTO stock_warehouses(code,name_ar) VALUES(?,?)",
            (f"WH-{bill_no}", "مخزن اختبار"),
        )
        warehouse_id = warehouse.lastrowid
        receipt_ids = []
        for index, amount in enumerate(receipt_amounts, start=1):
            cursor = conn.execute(
                """INSERT INTO goods_receipts(
                    receipt_no,supplier_id,warehouse_id,receipt_date,status,total_value
                ) VALUES(?,?,?,'2026-11-11','posted',?)""",
                (f"GR-{bill_no}-{index}", self.supplier_id, warehouse_id, str(amount)),
            )
            receipt_ids.append(cursor.lastrowid)
        tax = (Decimal(str(subtotal)) * Decimal(str(tax_rate)) / Decimal("100")).quantize(Decimal("0.01"))
        total = Decimal(str(subtotal)) + tax
        cursor = conn.execute(
            """INSERT INTO supplier_bills(
                bill_no,supplier_id,bill_date,due_date,supplier_invoice_no,subtotal,
                tax_rate,tax_amount,total_amount,created_by,status,bill_kind
            ) VALUES(?,?, '2026-11-11','2026-11-30',?,?,?,?,?,1,'approved','inventory')""",
            (bill_no, self.supplier_id, invoice_no, str(subtotal), str(tax_rate), str(tax), str(total)),
        )
        bill_id = cursor.lastrowid
        for receipt_id, amount in zip(receipt_ids, receipt_amounts):
            conn.execute(
                "INSERT INTO supplier_bill_receipts(bill_id,receipt_id,matched_amount) VALUES(?,?,?)",
                (bill_id, receipt_id, str(amount)),
            )
        conn.commit()
        conn.close()
        return bill_id

    def test_inventory_bill_posting_rejects_grni_mismatch_without_journal(self):
        bill_id = self._create_inventory_bill_with_receipts(
            "GRNI-MISMATCH", "INV-GRNI-MISMATCH", ["60.00", "40.00"], "110.00"
        )
        conn = purchases.db()
        with self.assertRaisesRegex(ValueError, "لا يطابق صافي فاتورة المورد"):
            purchases.post_supplier_bill(conn, bill_id, 2)
        conn.rollback()
        bill = conn.execute(
            "SELECT status,journal_entry_id FROM supplier_bills WHERE id=?", (bill_id,)
        ).fetchone()
        self.assertEqual(bill["status"], "approved")
        self.assertIsNone(bill["journal_entry_id"])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0], 0)
        conn.close()

    def test_inventory_bill_posts_multiple_receipts_and_input_vat_once(self):
        bill_id = self._create_inventory_bill_with_receipts(
            "GRNI-MULTI", "INV-GRNI-MULTI", ["60.00", "40.00"], "100.00", "14"
        )
        conn = purchases.db()
        entry_id = purchases.post_supplier_bill(conn, bill_id, 2)
        conn.commit()
        lines = conn.execute(
            """SELECT a.code,jl.debit,jl.credit FROM journal_lines jl
               JOIN accounts a ON a.id=jl.account_id WHERE jl.entry_id=? ORDER BY jl.id""",
            (entry_id,),
        ).fetchall()
        self.assertEqual(
            [(row["code"], Decimal(row["debit"]), Decimal(row["credit"])) for row in lines],
            [("2190", Decimal("60.00"), Decimal("0.00")),
             ("2110", Decimal("0.00"), Decimal("60.00")),
             ("2190", Decimal("40.00"), Decimal("0.00")),
             ("2110", Decimal("0.00"), Decimal("40.00")),
             ("1165", Decimal("14.00"), Decimal("0.00")),
             ("2110", Decimal("0.00"), Decimal("14.00"))],
        )
        self.assertEqual(sum((Decimal(row["debit"]) for row in lines), Decimal("0")),
                         sum((Decimal(row["credit"]) for row in lines), Decimal("0")))
        bill = conn.execute(
            "SELECT status,journal_entry_id FROM supplier_bills WHERE id=?", (bill_id,)
        ).fetchone()
        self.assertEqual(bill["status"], "posted")
        self.assertEqual(bill["journal_entry_id"], entry_id)
        conn.close()

    def test_expense_supplier_bill_posts_to_expense_and_payable(self):
        with self.client.session_transaction() as session:
            session["user_id"] = 1
            session["role"] = "accountant"
        response = self.client.post("/purchases/bills/new", data={
            "supplier_id": str(self.supplier_id),
            "supplier_invoice_no": "EXP-2026-001",
            "bill_date": "2026-11-11",
            "due_date": "2026-11-30",
            "bill_kind": "expense",
            "expense_account_id": str(self.expense_id),
            "subtotal": "100.00",
            "tax_rate": "0",
        })
        self.assertEqual(response.status_code, 302)
        conn = purchases.db()
        bill = conn.execute(
            "SELECT * FROM supplier_bills WHERE supplier_invoice_no='EXP-2026-001'"
        ).fetchone()
        self.assertIsNotNone(bill)
        self.assertEqual(bill["status"], "draft")
        self.assertIsNone(bill["journal_entry_id"])
        self.assertEqual(bill["due_date"], "2026-11-30")
        bill_id = bill["id"]
        conn.close()
        self._submit_approve_and_post(bill_id)
        conn = purchases.db()
        bill = conn.execute("SELECT * FROM supplier_bills WHERE id=?", (bill_id,)).fetchone()
        lines = conn.execute(
            "SELECT a.code,jl.debit,jl.credit FROM journal_lines jl "
            "JOIN accounts a ON a.id=jl.account_id WHERE jl.entry_id=? ORDER BY jl.id",
            (bill["journal_entry_id"],),
        ).fetchall()
        self.assertEqual(
            [
                (row["code"], Decimal(row["debit"]), Decimal(row["credit"]))
                for row in lines
            ],
            [
                ("5000", Decimal("100.00"), Decimal("0.00")),
                ("2110", Decimal("0.00"), Decimal("100.00")),
            ],
        )
        conn.close()


    def test_one_grni_receipt_can_be_split_across_two_posted_supplier_bills(self):
        conn = purchases.db()
        warehouse = conn.execute(
            "INSERT INTO stock_warehouses(code,name_ar) VALUES('WH-SPLIT','مخزن تقسيم')"
        )
        receipt = conn.execute("""INSERT INTO goods_receipts(
            receipt_no,supplier_id,warehouse_id,receipt_date,status,total_value
        ) VALUES('GR-SPLIT',?,?,'2026-11-13','posted','100.00')""",
            (self.supplier_id, warehouse.lastrowid))
        receipt_id = receipt.lastrowid
        conn.commit()
        conn.close()

        with self.client.session_transaction() as session:
            session["user_id"] = 1
            session["role"] = "accountant"
        for invoice_no, amount in (("INV-SPLIT-1", "40.00"), ("INV-SPLIT-2", "60.00")):
            response = self.client.post("/purchases/bills/new", data={
                "supplier_id": str(self.supplier_id),
                "supplier_invoice_no": invoice_no,
                "bill_date": "2026-11-13",
                "due_date": "2026-11-30",
                "bill_kind": "inventory",
                "subtotal": amount,
                "tax_rate": "0",
                "receipt_id": str(receipt_id),
                "receipt_amount_" + str(receipt_id): amount,
            })
            self.assertEqual(response.status_code, 302)
            conn = purchases.db()
            bill = conn.execute(
                "SELECT id,status,journal_entry_id FROM supplier_bills WHERE supplier_invoice_no=?",
                (invoice_no,)
            ).fetchone()
            self.assertIsNotNone(bill)
            self.assertEqual(bill["status"], "draft")
            self.assertIsNone(bill["journal_entry_id"])
            bill_id = bill["id"]
            allocation = conn.execute(
                "SELECT matched_amount FROM supplier_bill_receipts WHERE bill_id=? AND receipt_id=?",
                (bill_id, receipt_id)
            ).fetchone()
            self.assertEqual(Decimal(allocation["matched_amount"]), Decimal(amount))
            conn.close()
            self._submit_approve_and_post(bill_id)

        conn = purchases.db()
        allocations = conn.execute(
            "SELECT matched_amount FROM supplier_bill_receipts WHERE receipt_id=? ORDER BY bill_id",
            (receipt_id,)
        ).fetchall()
        self.assertEqual([Decimal(row["matched_amount"]) for row in allocations],
                         [Decimal("40.00"), Decimal("60.00")])
        posted = conn.execute(
            "SELECT COUNT(*) FROM supplier_bills WHERE supplier_invoice_no IN ('INV-SPLIT-1','INV-SPLIT-2') AND status='posted' AND journal_entry_id IS NOT NULL"
        ).fetchone()[0]
        self.assertEqual(posted, 2)
        grni_account = conn.execute("SELECT id FROM accounts WHERE code='2190'").fetchone()["id"]
        grni_debits = conn.execute(
            """SELECT debit FROM journal_lines jl JOIN journal_entries je ON je.id=jl.entry_id
               WHERE jl.account_id=? AND je.status='posted' ORDER BY jl.id""", (grni_account,)
        ).fetchall()
        self.assertEqual(sum((Decimal(row["debit"]) for row in grni_debits), Decimal("0")),
                         Decimal("100.00"))
        conn.execute("""INSERT INTO accounts(code,name_ar,account_type,is_group,active)
            VALUES('1000','الصندوق','asset',0,1)""")
        cash_account_id = conn.execute("SELECT id FROM accounts WHERE code='1000'").fetchone()["id"]
        conn.commit()
        conn.close()

        with self.client.session_transaction() as session:
            session["user_id"] = 1
            session["role"] = "accountant"
        payment_response = self.client.post("/purchases/payments/new", data={
            "supplier_id": str(self.supplier_id),
            "payment_date": "2026-11-13",
            "amount": "100.00",
            "cash_account_id": str(cash_account_id),
            "reference": "PAY-GRNI-SPLIT",
        })
        self.assertEqual(payment_response.status_code, 302)
        conn = purchases.db()
        payment = conn.execute(
            "SELECT amount,journal_entry_id FROM supplier_payments WHERE reference='PAY-GRNI-SPLIT'"
        ).fetchone()
        self.assertIsNotNone(payment)
        self.assertEqual(Decimal(payment["amount"]), Decimal("100.00"))
        allocations = conn.execute(
            """SELECT a.amount,b.supplier_invoice_no FROM supplier_payment_allocations a
               JOIN supplier_bills b ON b.id=a.bill_id
               WHERE a.payment_id=(SELECT id FROM supplier_payments WHERE reference='PAY-GRNI-SPLIT')
               ORDER BY b.supplier_invoice_no"""
        ).fetchall()
        self.assertEqual(
            [(row["supplier_invoice_no"], Decimal(row["amount"])) for row in allocations],
            [("INV-SPLIT-1", Decimal("40.00")), ("INV-SPLIT-2", Decimal("60.00"))],
        )
        payment_lines = conn.execute(
            """SELECT a.code,jl.debit,jl.credit FROM journal_lines jl
               JOIN accounts a ON a.id=jl.account_id WHERE jl.entry_id=? ORDER BY jl.id""",
            (payment["journal_entry_id"],),
        ).fetchall()
        self.assertEqual(
            [(row["code"], Decimal(row["debit"]), Decimal(row["credit"])) for row in payment_lines],
            [("2110", Decimal("100.00"), Decimal("0.00")),
             ("1000", Decimal("0.00"), Decimal("100.00"))],
        )
        paid_rows = conn.execute(
            "SELECT paid_amount FROM supplier_bills WHERE supplier_invoice_no IN ('INV-SPLIT-1','INV-SPLIT-2')"
        ).fetchall()
        paid_total = sum((Decimal(row["paid_amount"]) for row in paid_rows), Decimal("0"))
        self.assertEqual(paid_total, Decimal("100.00"))
        conn.close()


    def test_edit_bill_form_prefills_existing_grni_allocation(self):
        conn = purchases.db()
        warehouse = conn.execute(
            "INSERT INTO stock_warehouses(code,name_ar) VALUES('WH-EDIT-ALLOC','مخزن تعديل')"
        )
        receipt = conn.execute("""INSERT INTO goods_receipts(
            receipt_no,supplier_id,warehouse_id,receipt_date,status,total_value
        ) VALUES('GR-EDIT-ALLOC',?,?,'2026-11-13','posted','100.00')""",
            (self.supplier_id, warehouse.lastrowid))
        bill = conn.execute("""INSERT INTO supplier_bills(
            bill_no,supplier_id,bill_date,due_date,supplier_invoice_no,subtotal,
            tax_rate,tax_amount,total_amount,status,bill_kind
        ) VALUES('BILL-EDIT-ALLOC',?,'2026-11-13','2026-11-30','INV-EDIT-ALLOC',
            '40.00','0','0','40.00','draft','inventory')""", (self.supplier_id,))
        conn.execute(
            "INSERT INTO supplier_bill_receipts(bill_id,receipt_id,matched_amount) VALUES(?,?,?)",
            (bill.lastrowid, receipt.lastrowid, "40.00"),
        )
        conn.commit()
        bill_id = bill.lastrowid
        receipt_id = receipt.lastrowid
        conn.close()

        with self.client.session_transaction() as session:
            session["user_id"] = 1
            session["role"] = "accountant"
        with patch("purchases.render_template", side_effect=lambda _template, **ctx:
                   str(ctx["receipt_allocations"][receipt_id])) as render:
            response = self.client.get(f"/purchases/bills/{bill_id}/edit")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_data(as_text=True), "40.00")
        self.assertEqual(render.call_args.kwargs["selected_receipt_ids"], {receipt_id})



    def test_purchase_order_partial_receipts_grni_supplier_bill_end_to_end(self):
        conn = purchases.db()
        conn.execute("""INSERT INTO accounts(code,name_ar,account_type,is_group,active)
            VALUES('1150','مخزون بضائع','asset',0,1)""")
        product = conn.execute("""INSERT INTO stock_products(
            sku,name_ar,product_type,uom,purchase_price,active
        ) VALUES('PO-E2E-001','صنف اختبار دورة الشراء','goods','قطعة','10.00',1)""")
        product_id = product.lastrowid
        warehouse = conn.execute(
            "INSERT INTO stock_warehouses(code,name_ar) VALUES('WH-PO-E2E','مخزن دورة الشراء')"
        )
        warehouse_id = warehouse.lastrowid
        conn.commit()
        conn.close()

        with self.client.session_transaction() as session:
            session["user_id"] = 1
            session["role"] = "accountant"
        response = self.client.post("/purchases/orders/new", data={
            "supplier_id": str(self.supplier_id),
            "product_id": str(product_id),
            "quantity": "10",
            "unit_price": "10.00",
            "order_date": "2026-11-13",
            "expected_date": "2026-11-20",
            "notes": "اختبار استلام جزئي",
        })
        self.assertEqual(response.status_code, 302)
        conn = purchases.db()
        order = conn.execute(
            "SELECT id,status FROM purchase_orders ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(order["status"], "draft")
        order_id = order["id"]
        conn.close()

        response = self.client.post(f"/purchases/orders/{order_id}/submit")
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as session:
            session["user_id"] = 2
            session["role"] = "admin"
        response = self.client.post(
            f"/purchases/orders/{order_id}/approve",
            data={"reason": "مطابقة طلب الشراء"},
        )
        self.assertEqual(response.status_code, 302)

        receipt_ids = []
        for quantity in ("4", "6"):
            with self.client.session_transaction() as session:
                session["user_id"] = 3
                session["role"] = "accountant"
            response = self.client.post("/purchases/receipts/new", data={
                "supplier_id": str(self.supplier_id),
                "product_id": str(product_id),
                "warehouse_id": str(warehouse_id),
                "po_id": str(order_id),
                "quantity": quantity,
                "unit_cost": "10.00",
                "receipt_date": "2026-11-13",
            })
            self.assertEqual(response.status_code, 302)
            conn = purchases.db()
            receipt = conn.execute(
                "SELECT id,status,journal_entry_id,total_value FROM goods_receipts ORDER BY id DESC LIMIT 1"
            ).fetchone()
            self.assertEqual(receipt["status"], "posted")
            self.assertIsNotNone(receipt["journal_entry_id"])
            self.assertEqual(Decimal(receipt["total_value"]), Decimal(quantity) * Decimal("10.00"))
            receipt_ids.append(receipt["id"])
            conn.close()

        conn = purchases.db()
        order = conn.execute(
            "SELECT status FROM purchase_orders WHERE id=?", (order_id,)
        ).fetchone()
        order_line = conn.execute(
            "SELECT quantity,received_qty FROM purchase_order_lines WHERE po_id=?", (order_id,)
        ).fetchone()
        self.assertEqual(order["status"], "received")
        self.assertEqual(Decimal(order_line["quantity"]), Decimal("10"))
        self.assertEqual(Decimal(order_line["received_qty"]), Decimal("10"))
        self.assertEqual(
            stock.current_qty(conn, product_id, warehouse_id),
            Decimal("10"),
        )
        receipt_rows = conn.execute(
            "SELECT id,journal_entry_id FROM goods_receipts WHERE po_id=? ORDER BY id",
            (order_id,),
        ).fetchall()
        self.assertEqual(len(receipt_rows), 2)
        self.assertEqual([row["id"] for row in receipt_rows], receipt_ids)
        conn.close()

        # A further receipt against a fully received order must not create stock or GRNI.
        with patch("purchases.render_template", return_value="receipt form"):
            response = self.client.post("/purchases/receipts/new", data={
                "supplier_id": str(self.supplier_id),
                "product_id": str(product_id),
                "warehouse_id": str(warehouse_id),
                "po_id": str(order_id),
                "quantity": "1",
                "unit_cost": "10.00",
                "receipt_date": "2026-11-13",
            })
        # Validation failures re-render the receipt form (HTTP 200) and must
        # leave stock and GRNI untouched.
        self.assertEqual(response.status_code, 200)
        conn = purchases.db()
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM goods_receipts WHERE po_id=?", (order_id,)).fetchone()[0],
            2,
        )
        self.assertEqual(stock.current_qty(conn, product_id, warehouse_id), Decimal("10"))
        conn.close()

        response = self.client.post("/purchases/bills/new", data={
            "supplier_id": str(self.supplier_id),
            "supplier_invoice_no": "INV-PO-E2E-001",
            "bill_date": "2026-11-13",
            "due_date": "2026-11-30",
            "bill_kind": "inventory",
            "subtotal": "100.00",
            "tax_rate": "0",
            "receipt_id": [str(receipt_ids[0]), str(receipt_ids[1])],
            "receipt_amount_" + str(receipt_ids[0]): "40.00",
            "receipt_amount_" + str(receipt_ids[1]): "60.00",
        })
        self.assertEqual(response.status_code, 302)
        conn = purchases.db()
        bill = conn.execute(
            "SELECT id,status,journal_entry_id FROM supplier_bills WHERE supplier_invoice_no='INV-PO-E2E-001'"
        ).fetchone()
        self.assertIsNotNone(bill)
        self.assertEqual(bill["status"], "draft")
        self.assertIsNone(bill["journal_entry_id"])
        bill_id = bill["id"]
        conn.close()
        self._submit_approve_and_post(bill_id)

        conn = purchases.db()
        bill = conn.execute(
            "SELECT status,journal_entry_id FROM supplier_bills WHERE id=?", (bill_id,)
        ).fetchone()
        self.assertEqual(bill["status"], "posted")
        self.assertIsNotNone(bill["journal_entry_id"])
        allocation_total = conn.execute(
            "SELECT matched_amount FROM supplier_bill_receipts WHERE bill_id=? ORDER BY receipt_id",
            (bill_id,),
        ).fetchall()
        self.assertEqual(
            [Decimal(row["matched_amount"]) for row in allocation_total],
            [Decimal("40.00"), Decimal("60.00")],
        )
        grni_id = conn.execute("SELECT id FROM accounts WHERE code='2190'").fetchone()["id"]
        grni_net = conn.execute(
            """SELECT COALESCE(SUM(CAST(jl.credit AS REAL)-CAST(jl.debit AS REAL)),0)
               FROM journal_lines jl JOIN journal_entries je ON je.id=jl.entry_id
               WHERE jl.account_id=? AND je.status='posted'""",
            (grni_id,),
        ).fetchone()[0]
        self.assertEqual(Decimal(str(grni_net)), Decimal("0.0"))
        bill_lines = conn.execute(
            """SELECT a.code,jl.debit,jl.credit FROM journal_lines jl
               JOIN accounts a ON a.id=jl.account_id WHERE jl.entry_id=? ORDER BY jl.id""",
            (bill["journal_entry_id"],),
        ).fetchall()
        self.assertEqual(
            [(row["code"], Decimal(row["debit"]), Decimal(row["credit"])) for row in bill_lines],
            [("2190", Decimal("100.00"), Decimal("0.00")),
             ("2110", Decimal("0.00"), Decimal("100.00"))],
        )
        conn.close()


if __name__ == "__main__":
    unittest.main()
