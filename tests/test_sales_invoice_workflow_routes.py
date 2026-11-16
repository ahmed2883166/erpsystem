import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as nexus
import sales


class SalesInvoiceWorkflowRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original_path = sales.DB_PATH
        sales.DB_PATH = Path(self.temp.name) / "workflow.db"
        self.connection = self._connect()
        self.connection.executescript("""
            CREATE TABLE customers(id INTEGER PRIMARY KEY, name TEXT, code TEXT);
            INSERT INTO customers VALUES(1,'عميل اختبار','C-1');
            CREATE TABLE sales_invoice_workflows(
                id INTEGER PRIMARY KEY, workflow_no TEXT, customer_id INTEGER,
                status TEXT, created_by INTEGER, submitted_by INTEGER,
                approved_by INTEGER, approved_at TEXT,
                approved_credit_override INTEGER NOT NULL DEFAULT 0,
                rejected_by INTEGER, rejected_at TEXT, rejection_reason TEXT,
                updated_at TEXT
            );
        """)
        self.connection.commit()
        nexus.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        self.client = nexus.app.test_client()
        self.patch_db = patch.object(sales, "db", side_effect=self._connect)
        self.patch_db.start()

    def tearDown(self):
        self.patch_db.stop()
        self.connection.close()
        sales.DB_PATH = self.original_path
        self.temp.cleanup()

    def _connect(self):
        conn = sqlite3.connect(sales.DB_PATH)
        conn.row_factory = sqlite3.Row
        return conn

    def _workflow(self, status="pending_approval", created_by=10, submitted_by=10):
        cur = self.connection.execute(
            """INSERT INTO sales_invoice_workflows(
                workflow_no,customer_id,status,created_by,submitted_by
            ) VALUES('SIVD-TEST',1,?,?,?)""",
            (status, created_by, submitted_by),
        )
        self.connection.commit()
        return cur.lastrowid

    def _login(self, user_id=20, permissions=None, role="accountant"):
        with self.client.session_transaction() as session:
            session.update(user_id=user_id, username="reviewer",
                           role=role, permissions=permissions or ["sales.approve"])

    def test_invoice_creator_cannot_approve_own_workflow(self):
        workflow_id = self._workflow(created_by=20, submitted_by=20)
        self._login(user_id=20)
        response = self.client.post(
            f"/sales/invoices/workflow/{workflow_id}/decision",
            data={"action": "approve"},
        )
        self.assertEqual(response.status_code, 302)
        row = self.connection.execute(
            "SELECT status,approved_by FROM sales_invoice_workflows WHERE id=?",
            (workflow_id,),
        ).fetchone()
        self.assertEqual(tuple(row), ("pending_approval", None))

    def test_sales_manage_without_approval_permission_cannot_approve_invoice(self):
        workflow_id = self._workflow()
        self._login(permissions=["sales.manage"])
        response = self.client.post(
            f"/sales/invoices/workflow/{workflow_id}/decision",
            data={"action": "approve"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            self.connection.execute(
                "SELECT status FROM sales_invoice_workflows WHERE id=?", (workflow_id,)
            ).fetchone()["status"],
            "pending_approval",
        )

    def test_approval_permission_does_not_grant_posting_permission(self):
        workflow_id = self._workflow(status="approved")
        self._login(permissions=["sales.approve"])
        response = self.client.post(
            f"/sales/invoices/workflow/{workflow_id}/post",
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            self.connection.execute(
                "SELECT status,approved_by FROM sales_invoice_workflows WHERE id=?",
                (workflow_id,),
            ).fetchone()[:],
            ("approved", None),
        )

    def test_credit_override_flag_is_ignored_for_non_admin_approver(self):
        workflow_id = self._workflow()
        self._login(user_id=20, permissions=["sales.approve"], role="accountant")
        response = self.client.post(
            f"/sales/invoices/workflow/{workflow_id}/decision",
            data={"action": "approve", "credit_override": "1"},
        )
        self.assertEqual(response.status_code, 302)
        row = self.connection.execute(
            "SELECT status,approved_credit_override FROM sales_invoice_workflows WHERE id=?",
            (workflow_id,),
        ).fetchone()
        self.assertEqual(tuple(row), ("approved", 0))

    def test_admin_can_record_explicit_credit_override(self):
        workflow_id = self._workflow()
        self._login(user_id=99, permissions=["sales.approve"], role="admin")
        response = self.client.post(
            f"/sales/invoices/workflow/{workflow_id}/decision",
            data={"action": "approve", "credit_override": "1"},
        )
        self.assertEqual(response.status_code, 302)
        row = self.connection.execute(
            "SELECT status,approved_credit_override FROM sales_invoice_workflows WHERE id=?",
            (workflow_id,),
        ).fetchone()
        self.assertEqual(tuple(row), ("approved", 1))


if __name__ == "__main__":
    unittest.main()
