import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("NEXUS_SECRET_KEY", "unit-test-secret-key-which-is-long-enough-123")
os.environ["NEXUS_SKIP_DB_INIT"] = "1"

import app as nexus


class UserPermissionRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original_db_path = nexus.DB_PATH
        nexus.DB_PATH = Path(self.temp.name) / "users.db"
        conn = sqlite3.connect(nexus.DB_PATH)
        conn.execute("""CREATE TABLE users(
            id INTEGER PRIMARY KEY, username TEXT, full_name TEXT, role TEXT,
            active INTEGER, permissions_json TEXT, password_hash TEXT, created_at TEXT
        )""")
        conn.execute("""INSERT INTO users
            (id,username,full_name,role,active,permissions_json,password_hash,created_at)
            VALUES(1,'admin','مدير','admin',1,?,'hash','2026-01-01')""",
            (json.dumps(["users.manage"]),))
        conn.execute("""CREATE TABLE audit_log(
            id INTEGER PRIMARY KEY, user_id INTEGER, action TEXT, details TEXT,
            created_at TEXT, entity_type TEXT, entity_id TEXT, remote_addr TEXT,
            outcome TEXT NOT NULL DEFAULT 'success'
        )""")
        conn.commit()
        conn.close()
        nexus.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        self.client = nexus.app.test_client()

    def tearDown(self):
        nexus.DB_PATH = self.original_db_path
        self.temp.cleanup()

    def test_user_management_route_renders_real_users_page(self):
        with self.client.session_transaction() as session:
            session.update(user_id=1, username="admin", full_name="مدير",
                           role="admin", permissions=["users.manage"])
        response = self.client.get("/users")
        self.assertEqual(response.status_code, 200)
        self.assertIn("المستخدمون والصلاحيات", response.get_data(as_text=True))
        self.assertIn("admin", response.get_data(as_text=True))

    def test_viewer_cannot_open_or_post_user_permission_edit(self):
        with self.client.session_transaction() as session:
            session.update(user_id=1, username="admin", full_name="مدير",
                           role="viewer", permissions=["reports.view"])
        response = self.client.post("/users/1/edit", data={
            "username": "admin", "full_name": "مدير", "role": "admin",
            "active": "yes", "permissions": ["users.manage"],
        })
        self.assertEqual(response.status_code, 302)
        conn = sqlite3.connect(nexus.DB_PATH)
        role, permissions = conn.execute(
            "SELECT role,permissions_json FROM users WHERE id=1"
        ).fetchone()
        audit = conn.execute(
            "SELECT action,entity_type,outcome,details FROM audit_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        self.assertEqual(role, "admin")
        self.assertEqual(permissions, '["users.manage"]')
        self.assertEqual(audit[0], "module_operation")
        self.assertEqual(audit[1], "core")
        self.assertEqual(audit[2], "failure")
        self.assertIn("edit_user", audit[3])

    def test_user_cannot_change_own_role_or_permissions_even_with_admin_page_access(self):
        with self.client.session_transaction() as session:
            session.update(user_id=1, username="admin", full_name="مدير",
                           role="admin", permissions=["users.manage"])
        response = self.client.post("/users/1/edit", data={
            "username": "admin", "full_name": "مدير", "role": "viewer",
            "active": "yes", "permissions": ["reports.view"],
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn("لا يمكنك تغيير دورك أو صلاحياتك", response.get_data(as_text=True))
        conn = sqlite3.connect(nexus.DB_PATH)
        role, permissions = conn.execute(
            "SELECT role,permissions_json FROM users WHERE id=1"
        ).fetchone()
        conn.close()
        self.assertEqual(role, "admin")
        self.assertEqual(permissions, '["users.manage"]')


if __name__ == "__main__":
    unittest.main()
