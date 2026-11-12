import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import app as nexus_app


class AuditLogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "audit.db"
        self.connection = sqlite3.connect(self.db_path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("CREATE TABLE users(id INTEGER PRIMARY KEY, username TEXT, full_name TEXT)")
        self.connection.execute("INSERT INTO users VALUES(1,'admin','مدير النظام')")
        self.connection.execute("INSERT INTO users VALUES(17,'tester','مستخدم اختبار')")
        self.connection.execute("""
            CREATE TABLE audit_log(
                id INTEGER PRIMARY KEY, user_id INTEGER, action TEXT, details TEXT,
                created_at TEXT, entity_type TEXT, entity_id TEXT, remote_addr TEXT, outcome TEXT
            )
        """)
        self.connection.commit()

    def tearDown(self):
        self.connection.close()
        self.temp.cleanup()

    def test_log_records_authenticated_actor_and_request_context(self):
        with nexus_app.app.test_request_context("/", environ_base={"REMOTE_ADDR": "192.0.2.10"}):
            nexus_app.session["user_id"] = 17
            with patch.object(nexus_app, "db", side_effect=lambda: sqlite3.connect(self.db_path)):
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

        with nexus_app.app.test_request_context("/sales/invoices/44", method="POST"):
            nexus_app.session["user_id"] = 17
            nexus_app.request.url_rule = SimpleNamespace(endpoint="sales.test_mutation")
            nexus_app.request.view_args = {"invoice_id": 44}
            with patch.object(nexus_app, "db", side_effect=open_database):
                response = nexus_app.app.response_class("Saved", status=200)
                nexus_app.audit_operational_mutations(response)

        with nexus_app.app.test_request_context("/sales/invoices/45", method="POST"):
            nexus_app.session["user_id"] = 17
            nexus_app.request.endpoint = "sales.test_mutation"
            nexus_app.request.view_args = {"invoice_id": 45}
            with patch.object(nexus_app, "db", side_effect=open_database):
                response = nexus_app.app.response_class("Rejected", status=422)
                nexus_app.audit_operational_mutations(response)

        rows = self.connection.execute(
            "SELECT action,details,entity_type,entity_id,outcome FROM audit_log ORDER BY id"
        ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["action"], "module_operation")
        self.assertEqual(rows[0]["entity_type"], "sales")
        self.assertEqual(rows[0]["entity_id"], "44")
        self.assertEqual(rows[0]["outcome"], "success")
        self.assertEqual(rows[1]["entity_id"], "45")
        self.assertEqual(rows[1]["outcome"], "failure")

    def test_audit_view_is_a_separate_permission_and_route(self):
        self.assertIn("audit.view", nexus_app.PERMISSION_LABELS)
        self.assertIn("audit.view", nexus_app.PERMISSIONS["admin"])
        self.assertNotIn("audit.view", nexus_app.PERMISSIONS["accountant"])
        endpoints = {rule.endpoint for rule in nexus_app.app.url_map.iter_rules()}
        self.assertIn("audit_log", endpoints)


if __name__ == "__main__":
    unittest.main()
