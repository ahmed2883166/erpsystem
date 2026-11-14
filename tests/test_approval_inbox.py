import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as nexus


class ApprovalInboxTests(unittest.TestCase):
    def setUp(self):
        nexus.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "approvals.db"
        self._create_schema()
        self.client = nexus.app.test_client()

    def tearDown(self):
        self.temp.cleanup()

    def _connect(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _create_schema(self):
        conn = self._connect()
        conn.executescript("""
            CREATE TABLE users(id INTEGER PRIMARY KEY, username TEXT, full_name TEXT);
            INSERT INTO users VALUES(1,'reviewer','مراجع');
            CREATE TABLE suppliers(id INTEGER PRIMARY KEY, name TEXT);
            INSERT INTO suppliers VALUES(1,'مورد تجريبي');
            CREATE TABLE customers(id INTEGER PRIMARY KEY, name TEXT);
            INSERT INTO customers VALUES(1,'عميل تجريبي');
            CREATE TABLE purchase_orders(
                id INTEGER PRIMARY KEY, po_no TEXT, order_date TEXT, supplier_id INTEGER,
                status TEXT, created_by INTEGER, submitted_by INTEGER
            );
            INSERT INTO purchase_orders VALUES(1,'PO-1','2026-11-14',1,'pending_approval',2,2);
            INSERT INTO purchase_orders VALUES(2,'PO-2','2026-11-13',1,'pending_approval',1,1);
            CREATE TABLE supplier_bills(
                id INTEGER PRIMARY KEY, bill_no TEXT, bill_date TEXT, supplier_id INTEGER,
                total_amount TEXT, status TEXT, created_by INTEGER, submitted_by INTEGER
            );
            INSERT INTO supplier_bills VALUES(1,'BILL-1','2026-11-14',1,'120.00','pending_approval',2,2);
            CREATE TABLE sales_orders(
                id INTEGER PRIMARY KEY, order_no TEXT, order_date TEXT, customer_id INTEGER,
                status TEXT, created_by INTEGER
            );
            INSERT INTO sales_orders VALUES(1,'SO-1','2026-11-14',1,'pending_approval',2);
            CREATE TABLE budget_plans(
                id INTEGER PRIMARY KEY, name TEXT, date_from TEXT, date_to TEXT,
                status TEXT, created_by INTEGER
            );
            INSERT INTO budget_plans VALUES(1,'موازنة 2027','2027-01-01','2027-12-31','pending',2);
        """)
        conn.commit()
        conn.close()

    def test_inbox_collects_only_authorized_pending_documents_and_blocks_self_review(self):
        with self.client.session_transaction() as session:
            session.update(user_id=1, username="reviewer", full_name="مراجع",
                           role="accountant",
                           permissions=["purchases.approve", "sales.approve", "budget.approve"])
        with patch.object(nexus, "db", side_effect=self._connect):
            response = self.client.get("/approvals")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("PO-1", html)
        self.assertIn("BILL-1", html)
        self.assertIn("SO-1", html)
        self.assertIn("موازنة 2027", html)
        self.assertIn("لا يجوز اعتماد أو رفض المستند", html)
        self.assertIn("PO-2", html)
        self.assertEqual(html.count("فتح للمراجعة"), 4)

    def test_inbox_denies_users_without_approval_permissions(self):
        with self.client.session_transaction() as session:
            session.update(user_id=1, username="reviewer", full_name="مراجع",
                           role="viewer", permissions=["reports.view"])
        with patch.object(nexus, "db", side_effect=self._connect):
            response = self.client.get("/approvals")
        self.assertEqual(response.status_code, 302)


if __name__ == "__main__":
    unittest.main()
