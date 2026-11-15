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
            CREATE TABLE employees(id INTEGER PRIMARY KEY, full_name TEXT);
            INSERT INTO employees VALUES(1,'موظف تجريبي');
            CREATE TABLE payroll_runs(id INTEGER PRIMARY KEY,period TEXT,status TEXT,created_by INTEGER);
            CREATE TABLE payroll_lines(id INTEGER PRIMARY KEY,run_id INTEGER,net_pay TEXT);
            CREATE TABLE employee_final_payrolls(
                id INTEGER PRIMARY KEY,final_payroll_no TEXT,period TEXT,termination_date TEXT,
                net_pay TEXT,created_by INTEGER,journal_entry_id INTEGER,status TEXT
            );
            CREATE TABLE employee_loans(
                id INTEGER PRIMARY KEY,loan_no TEXT,request_date TEXT,requested_amount TEXT,
                created_by INTEGER,employee_id INTEGER,status TEXT
            );
            CREATE TABLE employee_grants(
                id INTEGER PRIMARY KEY,grant_name_snapshot TEXT,grant_date TEXT,total_amount TEXT,
                created_by INTEGER,journal_entry_id INTEGER,status TEXT
            );
            CREATE TABLE employee_end_of_service_settlements(
                id INTEGER PRIMARY KEY,settlement_no TEXT,termination_date TEXT,net_payable TEXT,
                created_by INTEGER,journal_entry_id INTEGER,status TEXT
            );
            CREATE TABLE friday_allowance_batches(
                id INTEGER PRIMARY KEY,period TEXT,total_amount TEXT,created_by INTEGER,
                approved_by INTEGER,status TEXT,journal_entry_id INTEGER
            );
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

    def test_inbox_includes_workforce_approvals_and_blocks_the_creator(self):
        conn = self._connect()
        conn.executescript("""
            INSERT INTO payroll_runs VALUES(1,'2026-11','draft',1);
            INSERT INTO payroll_lines VALUES(1,1,'15000.00');
            INSERT INTO employee_final_payrolls VALUES(1,'FP-1','2026-11','2026-11-15','9000',2,NULL,'draft');
            INSERT INTO employee_loans VALUES(1,'LOAN-1','2026-11-14','3000',2,1,'requested');
            INSERT INTO employee_grants VALUES(1,'منحة رمضان','2026-11-14','5000',2,NULL,'draft');
            INSERT INTO employee_end_of_service_settlements VALUES(1,'EOS-1','2026-11-15','12000',2,NULL,'draft');
            INSERT INTO friday_allowance_batches VALUES(1,'2026-11','2000',2,NULL,'draft',NULL);
        """)
        conn.commit()
        conn.close()
        with self.client.session_transaction() as session:
            session.update(user_id=1, username="reviewer", full_name="مراجع",
                           role="accountant", permissions=["workforce.manage"])
        with patch.object(nexus, "db", side_effect=self._connect):
            response = self.client.get("/approvals")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        for label in ("كشف المرتبات", "FP-1", "LOAN-1", "منحة رمضان", "EOS-1", "بدل الجمعة"):
            self.assertIn(label, html)
        self.assertIn("لا يجوز اعتماد وترحيل كشف المرتبات بواسطة منشئه", html)
        self.assertEqual(html.count("فتح للمراجعة"), 5)

    def test_inbox_denies_users_without_approval_permissions(self):
        with self.client.session_transaction() as session:
            session.update(user_id=1, username="reviewer", full_name="مراجع",
                           role="viewer", permissions=["reports.view"])
        with patch.object(nexus, "db", side_effect=self._connect):
            response = self.client.get("/approvals")
        self.assertEqual(response.status_code, 302)


if __name__ == "__main__":
    unittest.main()
