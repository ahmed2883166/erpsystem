import sqlite3
import unittest
from unittest.mock import patch

import app as nexus_app


class AuditLogTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("""
            CREATE TABLE audit_log(
                id INTEGER PRIMARY KEY, user_id INTEGER, action TEXT, details TEXT,
                created_at TEXT, entity_type TEXT, entity_id TEXT, remote_addr TEXT, outcome TEXT
            )
        """)

    def tearDown(self):
        self.connection.close()

    def test_log_records_authenticated_actor_and_request_context(self):
        with nexus_app.app.test_request_context("/", environ_base={"REMOTE_ADDR": "192.0.2.10"}):
            nexus_app.session["user_id"] = 17
            with patch.object(nexus_app, "db", return_value=self.connection):
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

    def test_audit_view_is_a_separate_permission_and_route(self):
        self.assertIn("audit.view", nexus_app.PERMISSION_LABELS)
        self.assertIn("audit.view", nexus_app.PERMISSIONS["admin"])
        self.assertNotIn("audit.view", nexus_app.PERMISSIONS["accountant"])
        endpoints = {rule.endpoint for rule in nexus_app.app.url_map.iter_rules()}
        self.assertIn("audit_log", endpoints)


if __name__ == "__main__":
    unittest.main()
