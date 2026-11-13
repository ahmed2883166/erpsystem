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

        response = self.client.get(f"/purchases/bills/{bill_id}/edit")
        self.assertEqual(response.status_code, 200)
        self.assertIn("EDIT-INV", response.get_data(as_text=True))

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


if __name__ == "__main__":
    unittest.main()
