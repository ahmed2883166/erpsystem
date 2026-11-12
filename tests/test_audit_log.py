import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import app as nexus_app
import report_exports as report_exports_module


class AuditLogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "audit.db"
        self.connection = sqlite3.connect(self.db_path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("CREATE TABLE users(id INTEGER PRIMARY KEY, username TEXT, full_name TEXT)")
        self.connection.execute("INSERT INTO users VALUES(1,'admin','مدير النظام')")
        self.connection.execute("INSERT INTO users VALUES(17,'tester','مستخدم اختبار')")
        self.connection.execute("CREATE TABLE company_settings(setting_key TEXT PRIMARY KEY, setting_value TEXT NOT NULL)")
        self.connection.execute("INSERT INTO company_settings VALUES('company_name','NEXUS Audit Test')")
        self.connection.execute("INSERT INTO company_settings VALUES('base_currency','EGP')")
        self.connection.execute("""
            CREATE TABLE audit_log(
                id INTEGER PRIMARY KEY, user_id INTEGER, action TEXT, details TEXT,
                created_at TEXT, entity_type TEXT, entity_id TEXT, remote_addr TEXT, outcome TEXT,
                actor_name TEXT, before_values TEXT, after_values TEXT, submitted_values TEXT, reason TEXT, request_id TEXT
            )
        """)
        self.connection.commit()

    def _open_connection(self):
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def tearDown(self):
        self.connection.close()
        self.temp.cleanup()

    def test_log_records_authenticated_actor_and_request_context(self):
        with nexus_app.app.test_request_context("/", environ_base={"REMOTE_ADDR": "192.0.2.10"}):
            nexus_app.session["user_id"] = 17
            with patch.object(nexus_app, "db", side_effect=self._open_connection):
                nexus_app.log(
                    "journal_posted", "قيد #42",
                    entity_type="journal", entity_id=42, outcome="success"
                )
        row = self.connection.execute("SELECT * FROM audit_log").fetchone()
        self.assertEqual(row["user_id"], 17)
        self.assertEqual(row["action"], "journal_posted")
        self.assertEqual(row["entity_type"], "journal")
        self.assertEqual(row["entity_id"], "42")
        self.assertEqual(row["remote_addr"], "192.0.2.10")
        self.assertEqual(row["outcome"], "success")
        self.assertNotIn("password", (row["details"] or "").lower())

    def test_audit_page_requires_permission_and_renders_log_rows(self):
        self.connection.execute(
            """INSERT INTO audit_log
               (user_id,action,details,created_at,entity_type,entity_id,remote_addr,outcome)
               VALUES(1,'journal_posted','قيد #42','2026-11-12T10:00:00+02:00','journal','42','192.0.2.10','success')"""
        )
        self.connection.commit()
        client = nexus_app.app.test_client()
        with client.session_transaction() as session:
            session.update(user_id=1, username="admin", full_name="مدير النظام",
                           role="admin", permissions=["audit.view"])
        def open_database():
            connection = sqlite3.connect(self.db_path)
            connection.row_factory = sqlite3.Row
            return connection
        with patch.object(nexus_app, "db", side_effect=open_database):
            response = client.get("/security/audit-log")
        self.assertEqual(response.status_code, 200)
        self.assertIn("journal_posted", response.get_data(as_text=True))
        with client.session_transaction() as session:
            session["permissions"] = []
        response = client.get("/security/audit-log")
        self.assertEqual(response.status_code, 302)

    def test_operational_mutation_hook_records_real_success_and_failure_outcomes(self):
        def open_database():
            connection = sqlite3.connect(self.db_path)
            connection.row_factory = sqlite3.Row
            return connection

        with nexus_app.app.test_request_context(
            "/sales/invoices/44", method="POST",
            data={"customer_name": "عميل تجريبي", "password": "do-not-store"},
        ):
            nexus_app.session["user_id"] = 17
            nexus_app.request.url_rule = SimpleNamespace(endpoint="sales.test_mutation")
            nexus_app.request.view_args = {"invoice_id": 44}
            with patch.object(nexus_app, "db", side_effect=open_database):
                response = nexus_app.app.response_class("Saved", status=200)
                nexus_app.audit_operational_mutations(response)

        with nexus_app.app.test_request_context("/sales/invoices/45", method="POST"):
            nexus_app.session["user_id"] = 17
            nexus_app.request.url_rule = SimpleNamespace(endpoint="sales.test_mutation")
            nexus_app.request.view_args = {"invoice_id": 45}
            with patch.object(nexus_app, "db", side_effect=open_database):
                nexus_app.flash("تعذر تنفيذ العملية بسبب بيانات غير صحيحة.", "error")
                response = nexus_app.app.response_class("", status=302)
                nexus_app.audit_operational_mutations(response)

        rows = self.connection.execute(
            "SELECT action,details,entity_type,entity_id,outcome,submitted_values FROM audit_log ORDER BY id"
        ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["action"], "module_operation")
        self.assertEqual(rows[0]["entity_type"], "sales")
        self.assertEqual(rows[0]["entity_id"], "44")
        self.assertEqual(rows[0]["outcome"], "success")
        self.assertIn("عميل تجريبي", rows[0]["submitted_values"])
        self.assertIn("[REDACTED]", rows[0]["submitted_values"])
        self.assertNotIn("do-not-store", rows[0]["submitted_values"])
        self.assertEqual(rows[1]["entity_id"], "45")
        self.assertEqual(rows[1]["outcome"], "failure")


    def test_audit_captures_transaction_and_custody_route_ids(self):
        def open_database():
            connection = sqlite3.connect(self.db_path)
            connection.row_factory = sqlite3.Row
            return connection

        with nexus_app.app.test_request_context("/bank/reconcile/91", method="POST"):
            nexus_app.session["user_id"] = 17
            nexus_app.request.url_rule = SimpleNamespace(endpoint="bank.reconcile")
            nexus_app.request.view_args = {"transaction_id": 91}
            with patch.object(nexus_app, "db", side_effect=open_database):
                response = nexus_app.app.response_class("Saved", status=200)
                nexus_app.audit_operational_mutations(response)

        row = self.connection.execute(
            "SELECT entity_type,entity_id,outcome FROM audit_log"
        ).fetchone()
        self.assertEqual(row["entity_type"], "bank")
        self.assertEqual(row["entity_id"], "91")
        self.assertEqual(row["outcome"], "success")

    def test_purchase_blueprint_mutations_are_included_in_operational_audit(self):
        def open_database():
            connection = sqlite3.connect(self.db_path)
            connection.row_factory = sqlite3.Row
            return connection

        with nexus_app.app.test_request_context(
            "/purchases/receipts/23", method="POST",
            data={"supplier_id": "5", "password": "must-not-be-stored"},
        ):
            nexus_app.session["user_id"] = 17
            nexus_app.request.url_rule = SimpleNamespace(endpoint="purchase.test_receipt")
            nexus_app.request.view_args = {"receipt_id": 23}
            with patch.object(nexus_app, "db", side_effect=open_database):
                response = nexus_app.app.response_class("Saved", status=200)
                nexus_app.audit_operational_mutations(response)

        row = self.connection.execute(
            "SELECT action,entity_type,entity_id,outcome,submitted_values FROM audit_log"
        ).fetchone()
        self.assertEqual(row["action"], "module_operation")
        self.assertEqual(row["entity_type"], "purchase")
        self.assertEqual(row["entity_id"], "23")
        self.assertEqual(row["outcome"], "success")
        self.assertIn("[REDACTED]", row["submitted_values"])
        self.assertNotIn("must-not-be-stored", row["submitted_values"])

    def test_audit_only_user_can_download_current_screen_pdf(self):
        client = nexus_app.app.test_client()
        with client.session_transaction() as session:
            session.update(user_id=17, username="tester", role="viewer", permissions=["audit.view"])

        def open_database(_db_path):
            connection = sqlite3.connect(self.db_path)
            connection.row_factory = sqlite3.Row
            return connection

        with patch.dict(nexus_app.app.config, {"WTF_CSRF_ENABLED": False}):
            with patch.object(report_exports_module, "connect_database", side_effect=open_database):
                response = client.post(
                    "/reports/export/current-screen/pdf",
                    json={"title": "سجل التدقيق", "tables": [{
                        "title": "العمليات",
                        "rows": [["العملية", "النتيجة"], ["journal_posted", "ناجحة"]],
                    }]},
                )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/pdf")
        self.assertTrue(response.data.startswith(b"%PDF"))

    def test_log_change_stores_before_after_reason_actor_and_request_id(self):
        with nexus_app.app.test_request_context(
            "/accounts/10/edit", method="POST", environ_base={"REMOTE_ADDR": "192.0.2.20"}
        ):
            nexus_app.session["user_id"] = 17
            nexus_app.g.audit_request_id = "request-test-001"
            with patch.object(nexus_app, "db", side_effect=self._open_connection):
                nexus_app.log_change(
                    "account_updated", "account", 10,
                    {"code": "1000", "name_ar": "الصندوق", "password": "old-secret"},
                    {"code": "1001", "name_ar": "الصندوق الرئيسي", "password": "new-secret"},
                    details="1001", reason="تصحيح اسم الحساب",
                )
        row = self.connection.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(row["actor_name"], "مستخدم اختبار")
        self.assertEqual(row["entity_id"], "10")
        self.assertEqual(row["reason"], "تصحيح اسم الحساب")
        self.assertEqual(row["request_id"], "request-test-001")
        self.assertIn('"code": "1000"', row["before_values"])
        self.assertIn('"code": "1001"', row["after_values"])
        self.assertIn("[REDACTED]", row["before_values"])
        self.assertNotIn("old-secret", row["before_values"])
        self.assertEqual(row["remote_addr"], "192.0.2.20")

    def test_audit_view_is_a_separate_permission_and_route(self):
        self.assertIn("audit.view", nexus_app.PERMISSION_LABELS)
        self.assertIn("audit.view", nexus_app.PERMISSIONS["admin"])
        self.assertNotIn("audit.view", nexus_app.PERMISSIONS["accountant"])
        endpoints = {rule.endpoint for rule in nexus_app.app.url_map.iter_rules()}
        self.assertIn("audit_log", endpoints)


if __name__ == "__main__":
    unittest.main()
