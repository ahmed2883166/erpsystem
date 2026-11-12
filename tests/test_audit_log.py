import sqlite3
import tempfile
import unittest
from pathlib import Path
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

    def test_audit_view_is_a_separate_permission_and_route(self):
        self.assertIn("audit.view", nexus_app.PERMISSION_LABELS)
        self.assertIn("audit.view", nexus_app.PERMISSIONS["admin"])
        self.assertNotIn("audit.view", nexus_app.PERMISSIONS["accountant"])
        endpoints = {rule.endpoint for rule in nexus_app.app.url_map.iter_rules()}
        self.assertIn("audit_log", endpoints)


if __name__ == "__main__":
    unittest.main()
