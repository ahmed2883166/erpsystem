import tempfile
import unittest
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
        self.app.secret_key = "test-secret"
        self.app.register_blueprint(purchases.purchase_bp)
        self.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        CSRFProtect(self.app)
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

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
        response = self.client.get("/purchases/integrity")
        self.assertEqual(response.status_code, 200)
        self.assertIn("LEGACY-1", response.get_data(as_text=True))
        self.assertIn("LEGACY-2", response.get_data(as_text=True))

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
        self.assertEqual(bill["due_date"], "2026-11-30")
        lines = conn.execute(
            "SELECT a.code,jl.debit,jl.credit FROM journal_lines jl "
            "JOIN accounts a ON a.id=jl.account_id WHERE jl.entry_id=? ORDER BY jl.id",
            (bill["journal_entry_id"],),
        ).fetchall()
        self.assertEqual(
            [(row["code"], row["debit"], row["credit"]) for row in lines],
            [("5000", "100.00", "0"), ("2110", "0", "100.00")],
        )
        conn.close()


if __name__ == "__main__":
    unittest.main()
