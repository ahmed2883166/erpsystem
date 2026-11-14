import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as nexus
import sales


class SalesDiscountApprovalRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original_sales_db_path = sales.DB_PATH
        sales.DB_PATH = Path(self.temp.name) / "sales-approval.db"
        self.connection = sqlite3.connect(sales.DB_PATH)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript("""
            CREATE TABLE customers(
                id INTEGER PRIMARY KEY, code TEXT, name TEXT
            );
            INSERT INTO customers VALUES(1,'C-1','عميل تجريبي');
            CREATE TABLE sales_invoices(id INTEGER PRIMARY KEY, due_date TEXT);
            CREATE TABLE sales_orders(
                id INTEGER PRIMARY KEY, status TEXT, discount_approval_request_id INTEGER,
                discount_approval_status TEXT, rejection_reason TEXT,
                rejected_by INTEGER, rejected_at TEXT
            );
            CREATE TABLE sales_discount_approval_requests(
                id INTEGER PRIMARY KEY, request_no TEXT, customer_id INTEGER,
                invoice_date TEXT, due_date TEXT, gross_subtotal TEXT,
                discount_amount TEXT, discount_percent TEXT, payload_json TEXT,
                created_by INTEGER, status TEXT DEFAULT 'pending', reviewed_by INTEGER,
                reviewed_at TEXT, decision_reason TEXT, invoice_id INTEGER,
                request_type TEXT DEFAULT 'invoice', sales_order_id INTEGER
            );
        """)
        self.connection.commit()
        nexus.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        self.client = nexus.app.test_client()

    def tearDown(self):
        self.connection.close()
        sales.DB_PATH = self.original_sales_db_path
        self.temp.cleanup()

    def _connect(self):
        conn = sqlite3.connect(sales.DB_PATH)
        conn.row_factory = sqlite3.Row
        return conn

    def _add_request(self, request_type="invoice", created_by=10, sales_order_id=None):
        payload = {
            "customer_id": 1, "invoice_date": "2026-11-14", "due_date": "2026-11-30",
            "warehouse_id": 1, "items": [{"product_id": 1, "quantity": "1", "unit_price": "100"}],
            "tax_rate": "14", "discount_override": {
                "discount_type": "percent", "discount_value": "20", "discount_amount": "20"
            },
        }
        cursor = self.connection.execute(
            """INSERT INTO sales_discount_approval_requests(
                request_no,customer_id,invoice_date,due_date,gross_subtotal,
                discount_amount,discount_percent,payload_json,created_by,status,
                request_type,sales_order_id
            ) VALUES('SDR-TEST',1,'2026-11-14','2026-11-30','100','20','20',?,?,'pending',?,?)""",
            (json.dumps(payload), created_by, request_type, sales_order_id),
        )
        request_id = cursor.lastrowid
        if request_type == "sales_order":
            self.connection.execute(
                """INSERT INTO sales_orders(
                    id,status,discount_approval_request_id,discount_approval_status
                ) VALUES(?, 'pending_approval', ?, 'pending')""",
                (sales_order_id, request_id),
            )
        self.connection.commit()
        return request_id

    def _login(self, user_id=20, permissions=None, role="accountant"):
        with self.client.session_transaction() as session:
            session.update(
                user_id=user_id, username="reviewer", full_name="مراجع",
                role=role, permissions=permissions or ["sales.approve"],
            )

    def test_invoice_discount_approval_posts_invoice_once_and_links_decision(self):
        request_id = self._add_request()
        def fake_post_invoice(conn, *args, **kwargs):
            conn.execute("INSERT INTO sales_invoices(id,due_date) VALUES(71,NULL)")
            return {"invoice_id": 71, "invoice_no": "INV-71"}

        with patch.object(sales, "db", side_effect=self._connect), \
             patch.object(sales, "post_invoice", side_effect=fake_post_invoice) as post, \
             patch.object(nexus, "log"):
            self._login()
            response = self.client.post(
                f"/sales/discount-approvals/{request_id}",
                data={"action": "approve", "reason": "موافقة المدير"},
            )
        self.assertEqual(response.status_code, 302)
        post.assert_called_once()
        request_row = self.connection.execute(
            "SELECT status,reviewed_by,decision_reason,invoice_id FROM sales_discount_approval_requests WHERE id=?",
            (request_id,),
        ).fetchone()
        self.assertEqual(tuple(request_row), ("approved", 20, "موافقة المدير", 71))
        self.assertEqual(
            self.connection.execute("SELECT due_date FROM sales_invoices WHERE id=71").fetchone()["due_date"],
            "2026-11-30",
        )

    def test_invoice_discount_rejection_does_not_post_invoice_or_create_invoice(self):
        request_id = self._add_request()
        with patch.object(sales, "db", side_effect=self._connect), \
             patch.object(sales, "post_invoice") as post, \
             patch.object(nexus, "log"):
            self._login()
            response = self.client.post(
                f"/sales/discount-approvals/{request_id}",
                data={"action": "reject", "reason": "الخصم غير معتمد"},
            )
        self.assertEqual(response.status_code, 302)
        post.assert_not_called()
        request_row = self.connection.execute(
            "SELECT status,reviewed_by,decision_reason,invoice_id FROM sales_discount_approval_requests WHERE id=?",
            (request_id,),
        ).fetchone()
        self.assertEqual(tuple(request_row), ("rejected", 20, "الخصم غير معتمد", None))
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM sales_invoices").fetchone()[0], 0)

    def test_sales_order_discount_approval_unlocks_only_discount_gate(self):
        request_id = self._add_request("sales_order", sales_order_id=31)
        with patch.object(sales, "db", side_effect=self._connect), patch.object(nexus, "log"):
            self._login()
            response = self.client.post(
                f"/sales/discount-approvals/{request_id}",
                data={"action": "approve", "reason": "موافقة الخصم"},
            )
        self.assertEqual(response.status_code, 302)
        request_row = self.connection.execute(
            "SELECT status,reviewed_by FROM sales_discount_approval_requests WHERE id=?",
            (request_id,),
        ).fetchone()
        order = self.connection.execute(
            "SELECT status,discount_approval_status FROM sales_orders WHERE id=31"
        ).fetchone()
        self.assertEqual(tuple(request_row), ("approved", 20))
        self.assertEqual(tuple(order), ("pending_approval", "approved"))

    def test_sales_order_discount_rejection_rejects_order_without_posting(self):
        request_id = self._add_request("sales_order", sales_order_id=32)
        with patch.object(sales, "db", side_effect=self._connect), \
             patch.object(sales, "post_invoice") as post, \
             patch.object(nexus, "log"):
            self._login()
            response = self.client.post(
                f"/sales/discount-approvals/{request_id}",
                data={"action": "reject", "reason": "السعر غير مناسب"},
            )
        self.assertEqual(response.status_code, 302)
        post.assert_not_called()
        order = self.connection.execute(
            "SELECT status,discount_approval_status,rejected_by,rejection_reason FROM sales_orders WHERE id=32"
        ).fetchone()
        self.assertEqual(tuple(order), ("rejected", "rejected", 20, "السعر غير مناسب"))

    def test_request_creator_cannot_approve_or_reject_own_discount(self):
        request_id = self._add_request(created_by=20)
        with patch.object(sales, "db", side_effect=self._connect):
            self._login(user_id=20)
            response = self.client.post(
                f"/sales/discount-approvals/{request_id}",
                data={"action": "approve", "reason": "محاولة اعتماد ذاتي"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn("لا يجوز اعتماد أو رفض طلب الخصم الذي أنشأته بنفسك", response.get_data(as_text=True))
        self.assertEqual(
            self.connection.execute(
                "SELECT status,reviewed_by FROM sales_discount_approval_requests WHERE id=?",
                (request_id,),
            ).fetchone()[:],
            ("pending", None),
        )

    def test_sales_manage_without_sales_approve_cannot_review_discount(self):
        request_id = self._add_request()
        with patch.object(sales, "db", side_effect=self._connect):
            self._login(permissions=["sales.manage"])
            response = self.client.post(
                f"/sales/discount-approvals/{request_id}",
                data={"action": "approve", "reason": "غير مصرح"},
            )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            self.connection.execute(
                "SELECT status FROM sales_discount_approval_requests WHERE id=?", (request_id,)
            ).fetchone()["status"],
            "pending",
        )


if __name__ == "__main__":
    unittest.main()
