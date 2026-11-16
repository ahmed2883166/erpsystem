import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask
import purchases


class PurchaseRequestWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "purchase_requests.db"
        self._create_schema()
        self.app = Flask(__name__)
        self.app.secret_key = "test-only"
        self.app.register_blueprint(purchases.purchase_bp)
        self.client = self.app.test_client()

    def tearDown(self):
        self.temp.cleanup()

    def _connect(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _create_schema(self):
        conn = self._connect()
        conn.executescript("""
            CREATE TABLE stock_products(
                id INTEGER PRIMARY KEY, sku TEXT NOT NULL, name_ar TEXT NOT NULL,
                uom TEXT DEFAULT 'قطعة', active INTEGER NOT NULL DEFAULT 1
            );
            INSERT INTO stock_products VALUES(1,'ITEM-1','صنف اختبار','قطعة',1);
            CREATE TABLE suppliers(
                id INTEGER PRIMARY KEY, name TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1
            );
            INSERT INTO suppliers VALUES(1,'مورد اختبار',1);
            CREATE TABLE purchase_orders(
                id INTEGER PRIMARY KEY, po_no TEXT NOT NULL UNIQUE,
                supplier_id INTEGER NOT NULL, order_date TEXT NOT NULL,
                expected_date TEXT, notes TEXT NOT NULL DEFAULT '',
                created_by INTEGER, status TEXT NOT NULL DEFAULT 'draft'
            );
            CREATE TABLE purchase_order_lines(
                id INTEGER PRIMARY KEY, po_id INTEGER NOT NULL,
                product_id INTEGER NOT NULL, quantity TEXT NOT NULL,
                unit_price TEXT NOT NULL, received_qty TEXT NOT NULL DEFAULT '0'
            );
            CREATE TABLE purchase_requests(
                id INTEGER PRIMARY KEY, request_no TEXT NOT NULL UNIQUE,
                request_date TEXT NOT NULL, required_date TEXT,
                department TEXT NOT NULL DEFAULT '', purpose TEXT NOT NULL,
                product_id INTEGER NOT NULL, quantity TEXT NOT NULL,
                estimated_unit_price TEXT NOT NULL DEFAULT '0',
                status TEXT NOT NULL DEFAULT 'draft',
                created_by INTEGER NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                submitted_by INTEGER, submitted_at TEXT,
                approved_by INTEGER, approved_at TEXT, approval_reason TEXT,
                rejected_by INTEGER, rejected_at TEXT, rejection_reason TEXT,
                converted_to_po_id INTEGER, converted_at TEXT
            );
        """)
        conn.commit()
        conn.close()

    def _login(self, user_id, permissions):
        with self.client.session_transaction() as session:
            session["user_id"] = user_id
            session["role"] = "accountant" if user_id == 1 else "admin"
            session["permissions"] = permissions

    def test_request_requires_valid_data_and_creates_no_accounting_or_stock_entries(self):
        self._login(1, ["purchases.manage", "purchases.view"])
        with patch.object(purchases, "db", side_effect=self._connect):
            response = self.client.post("/purchases/requests/new", data={
                "product_id": "1", "quantity": "3", "estimated_unit_price": "25.50",
                "request_date": "2026-11-14", "required_date": "2026-11-20",
                "department": "الصيانة", "purpose": "احتياج تشغيل وصيانة"
            })
        self.assertEqual(response.status_code, 302)
        conn = self._connect()
        row = conn.execute("SELECT * FROM purchase_requests").fetchone()
        self.assertEqual(row["status"], "draft")
        self.assertEqual(row["created_by"], 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM purchase_orders").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM purchase_order_lines").fetchone()[0], 0)
        conn.close()

    def test_self_approval_is_blocked_and_approved_request_converts_once_to_draft_po(self):
        self._login(1, ["purchases.manage", "purchases.view", "purchases.approve"])
        with patch.object(purchases, "db", side_effect=self._connect):
            self.client.post("/purchases/requests/new", data={
                "product_id": "1", "quantity": "3", "estimated_unit_price": "25.50",
                "request_date": "2026-11-14", "department": "الصيانة",
                "purpose": "احتياج تشغيل وصيانة"
            })
            conn = self._connect()
            request_id = conn.execute("SELECT id FROM purchase_requests").fetchone()["id"]
            conn.close()
            self.client.post(f"/purchases/requests/{request_id}/submit")
            self.client.post(f"/purchases/requests/{request_id}/approve")
            conn = self._connect()
            self.assertEqual(conn.execute(
                "SELECT status FROM purchase_requests WHERE id=?", (request_id,)
            ).fetchone()["status"], "pending_approval")
            conn.close()

            self._login(2, ["purchases.manage", "purchases.view", "purchases.approve"])
            self.client.post(f"/purchases/requests/{request_id}/approve", data={"reason": "تمت المراجعة"})
            self.client.post(f"/purchases/requests/{request_id}/convert", data={
                "supplier_id": "1", "unit_price": "24.75"
            })
            self.client.post(f"/purchases/requests/{request_id}/convert", data={
                "supplier_id": "1", "unit_price": "24.75"
            })

        conn = self._connect()
        request_row = conn.execute("SELECT * FROM purchase_requests WHERE id=?", (request_id,)).fetchone()
        self.assertEqual(request_row["status"], "converted")
        self.assertIsNotNone(request_row["converted_to_po_id"])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM purchase_orders").fetchone()[0], 1)
        po = conn.execute("SELECT * FROM purchase_orders").fetchone()
        self.assertEqual(po["status"], "draft")
        line = conn.execute("SELECT * FROM purchase_order_lines").fetchone()
        self.assertEqual(line["quantity"], "3")
        self.assertEqual(line["unit_price"], "24.75")
        conn.close()


if __name__ == "__main__":
    unittest.main()
