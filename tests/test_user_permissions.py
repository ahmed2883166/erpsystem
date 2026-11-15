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
            active INTEGER, permissions_json TEXT, password_hash TEXT, created_at TEXT,
            must_change_password INTEGER NOT NULL DEFAULT 0
        )""")
        conn.execute("""INSERT INTO users
            (id,username,full_name,role,active,permissions_json,password_hash,created_at)
            VALUES(1,'admin','مدير','admin',1,?,'hash','2026-01-01')""",
            (json.dumps(["users.manage"]),))
        conn.execute("""CREATE TABLE audit_log(
            id INTEGER PRIMARY KEY, user_id INTEGER, action TEXT, details TEXT,
            created_at TEXT, entity_type TEXT, entity_id TEXT, remote_addr TEXT,
            outcome TEXT NOT NULL DEFAULT 'success',
            actor_name TEXT, before_values TEXT, after_values TEXT, submitted_values TEXT, reason TEXT, request_id TEXT
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
        conn = sqlite3.connect(nexus.DB_PATH)
        conn.execute("""INSERT INTO users
            (id,username,full_name,role,active,permissions_json,password_hash,created_at)
            VALUES(2,'viewer','مستخدم عرض','viewer',1,?,'hash','2026-01-01')""",
            (json.dumps(["reports.view"]),))
        conn.commit()
        conn.close()
        with self.client.session_transaction() as session:
            session.update(user_id=2, username="viewer", full_name="مستخدم عرض",
                           role="viewer", permissions=["reports.view"])
        response = self.client.post("/users/1/edit", data={
            "username": "admin", "full_name": "مدير", "role": "viewer",
            "active": "yes", "permissions": ["reports.view"],
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

    def test_admin_can_create_custom_role_and_new_user_inherits_role_permissions(self):
        with self.client.session_transaction() as session:
            session.update(user_id=1, username="admin", full_name="مدير",
                           role="admin", permissions=["users.manage"])
        response = self.client.post("/users/roles", data={
            "action": "create", "role_name": "مدير المبيعات",
            "role_permissions": ["sales.view", "sales.manage", "sales.approve"],
        })
        self.assertEqual(response.status_code, 302)
        conn = sqlite3.connect(nexus.DB_PATH)
        role_key, role_json = conn.execute(
            "SELECT role_key,permissions_json FROM roles WHERE role_name='مدير المبيعات'"
        ).fetchone()
        conn.close()
        role_permissions = json.loads(role_json)
        self.assertIn("sales.view", role_permissions)
        self.assertIn("sales.manage", role_permissions)
        self.assertIn("sales.approve", role_permissions)

        response = self.client.post("/users", data={
            "username": "sales.manager", "full_name": "مدير مبيعات",
            "password": "long-password-123", "role": role_key,
            "permissions": role_permissions,
        })
        self.assertEqual(response.status_code, 200)
        conn = sqlite3.connect(nexus.DB_PATH)
        saved_role, saved_permissions = conn.execute(
            "SELECT role,permissions_json FROM users WHERE username='sales.manager'"
        ).fetchone()
        conn.close()
        self.assertEqual(saved_role, role_key)
        self.assertIsNone(saved_permissions)
        self.assertEqual(nexus.stored_user_permissions(saved_permissions, saved_role), role_permissions)

    def test_custom_role_updates_are_audited_and_assigned_roles_cannot_be_disabled(self):
        with self.client.session_transaction() as session:
            session.update(user_id=1, username="admin", full_name="مدير",
                           role="admin", permissions=["users.manage"])
        self.client.post("/users/roles", data={
            "action": "create", "role_name": "مراجع المبيعات",
            "role_permissions": ["sales.view", "sales.manage"],
        })
        conn = sqlite3.connect(nexus.DB_PATH)
        role_key = conn.execute(
            "SELECT role_key FROM roles WHERE role_name='مراجع المبيعات'"
        ).fetchone()[0]
        conn.close()
        self.client.post("/users", data={
            "username": "sales.reviewer", "full_name": "مراجع",
            "password": "long-password-123", "role": role_key,
            "permissions": ["sales.view", "sales.manage"],
        })
        conn = sqlite3.connect(nexus.DB_PATH)
        user_id = conn.execute(
            "SELECT id FROM users WHERE username='sales.reviewer'"
        ).fetchone()[0]
        conn.close()

        updated = self.client.post("/users/roles", data={
            "action": "update", "role_key": role_key,
            "role_name": "مراجع مبيعات أول",
            "role_permissions": ["sales.view"],
        })
        self.assertEqual(updated.status_code, 302)
        self.assertEqual(nexus.stored_user_permissions(None, role_key), ["sales.view"])

        self.client.post("/users/roles", data={
            "action": "deactivate", "role_key": role_key,
        })
        conn = sqlite3.connect(nexus.DB_PATH)
        active = conn.execute("SELECT active FROM roles WHERE role_key=?", (role_key,)).fetchone()[0]
        conn.close()
        self.assertEqual(active, 1)

        self.client.post(f"/users/{user_id}/edit", data={
            "username": "sales.reviewer", "full_name": "مراجع",
            "role": "viewer", "active": "yes",
            "permissions": sorted(nexus.PERMISSIONS["viewer"]),
        })
        self.client.post("/users/roles", data={
            "action": "deactivate", "role_key": role_key,
        })
        conn = sqlite3.connect(nexus.DB_PATH)
        active = conn.execute("SELECT active FROM roles WHERE role_key=?", (role_key,)).fetchone()[0]
        audit_actions = [row[0] for row in conn.execute(
            "SELECT action FROM audit_log WHERE entity_type='role' ORDER BY id"
        )]
        conn.close()
        self.assertEqual(active, 0)
        self.assertIn("role_updated", audit_actions)
        self.assertIn("role_deactivated", audit_actions)

    def test_workforce_manage_and_approval_permissions_are_independent(self):
        manager = nexus.normalize_user_permissions(["workforce.manage"])
        self.assertIn("workforce.view", manager)
        self.assertIn("workforce.manage", manager)
        self.assertNotIn("workforce.approve", manager)

        approver = nexus.normalize_user_permissions(["workforce.approve"])
        self.assertIn("workforce.view", approver)
        self.assertIn("workforce.approve", approver)
        self.assertNotIn("workforce.manage", approver)

        self.assertIn("workforce.approve", nexus.PERMISSIONS["admin"])
        self.assertIn("workforce.approve", nexus.PERMISSIONS["accountant"])
        self.assertNotIn("workforce.approve", nexus.PERMISSIONS["viewer"])

    def test_sales_manage_does_not_implicitly_grant_discount_approval(self):
        permissions = nexus.normalize_user_permissions(["sales.manage"])
        self.assertIn("sales.view", permissions)
        self.assertIn("sales.manage", permissions)
        self.assertNotIn("sales.approve", permissions)

        approval_only = nexus.normalize_user_permissions(["sales.approve"])
        self.assertIn("sales.approve", approval_only)
        self.assertNotIn("sales.manage", approval_only)

    def test_non_admin_cannot_manage_custom_roles_even_if_users_manage_is_granted(self):
        conn = sqlite3.connect(nexus.DB_PATH)
        conn.execute("UPDATE users SET role='accountant',permissions_json=? WHERE id=1",
                     (json.dumps(["users.manage"]),))
        conn.commit()
        conn.close()
        with self.client.session_transaction() as session:
            session.update(user_id=1, username="admin", full_name="مدير",
                           role="accountant", permissions=["users.manage"])
        response = self.client.post("/users/roles", data={
            "action": "create", "role_name": "دور غير مصرح",
            "role_permissions": ["users.manage"],
        })
        self.assertEqual(response.status_code, 302)
        conn = sqlite3.connect(nexus.DB_PATH)
        count = conn.execute("SELECT COUNT(*) FROM roles WHERE role_name='دور غير مصرح'").fetchone()[0]
        conn.close()
        self.assertEqual(count, 0)

    def test_non_admin_user_page_hides_admin_role_and_unowned_permissions(self):
        conn = sqlite3.connect(nexus.DB_PATH)
        conn.execute(
            "UPDATE users SET role='accountant',permissions_json=? WHERE id=1",
            (json.dumps(["users.manage"]),),
        )
        conn.commit()
        conn.close()
        with self.client.session_transaction() as session:
            session.update(user_id=1, username="admin", full_name="مدير",
                           role="accountant", permissions=["users.manage"])
        response = self.client.get("/users")
        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('<option value="admin">', body)
        self.assertNotIn('name="permissions" value="backup.manage"', body)
        self.assertIn('name="permissions" value="users.manage"', body)

    def test_non_admin_user_manager_cannot_create_admin_or_grant_extra_permissions(self):
        conn = sqlite3.connect(nexus.DB_PATH)
        conn.execute(
            "UPDATE users SET role='accountant',permissions_json=? WHERE id=1",
            (json.dumps(["users.manage"]),),
        )
        conn.commit()
        conn.close()
        with self.client.session_transaction() as session:
            session.update(user_id=1, username="admin", full_name="مدير",
                           role="accountant", permissions=["users.manage"])

        response = self.client.post("/users", data={
            "username": "escalated", "full_name": "مستخدم بصلاحيات مدير",
            "password": "long-password-123", "role": "admin",
            "permissions": ["users.manage", "backup.manage"],
        })
        self.assertEqual(response.status_code, 200)
        conn = sqlite3.connect(nexus.DB_PATH)
        created = conn.execute(
            "SELECT id FROM users WHERE username='escalated'"
        ).fetchone()
        conn.close()
        self.assertIsNone(created)
        self.assertIn("مدير النظام فقط", response.get_data(as_text=True))

    def test_non_admin_user_manager_cannot_edit_or_delete_higher_privilege_users(self):
        conn = sqlite3.connect(nexus.DB_PATH)
        conn.execute(
            "UPDATE users SET role='accountant',permissions_json=? WHERE id=1",
            (json.dumps(["users.manage"]),),
        )
        conn.execute(
            """INSERT INTO users
               (id,username,full_name,role,active,permissions_json,password_hash,created_at)
               VALUES(2,'privileged','مستخدم أعلى','viewer',1,?,'hash','2026-01-01')""",
            (json.dumps(["reports.view", "backup.manage"]),),
        )
        conn.execute(
            """INSERT INTO users
               (id,username,full_name,role,active,permissions_json,password_hash,created_at)
               VALUES(3,'protected-admin','مدير محمي','admin',1,?,'hash','2026-01-01')""",
            (json.dumps(["users.manage"]),),
        )
        conn.commit()
        conn.close()
        with self.client.session_transaction() as session:
            session.update(user_id=1, username="admin", full_name="مدير",
                           role="accountant", permissions=["users.manage"])

        edited = self.client.post("/users/2/edit", data={
            "username": "privileged", "full_name": "مستخدم أعلى",
            "role": "viewer", "active": "yes", "permissions": ["users.manage"],
        })
        self.assertEqual(edited.status_code, 302)
        deleted = self.client.post("/users/3/delete")
        self.assertEqual(deleted.status_code, 302)

        conn = sqlite3.connect(nexus.DB_PATH)
        target = conn.execute(
            "SELECT role,active,permissions_json FROM users WHERE id=2"
        ).fetchone()
        protected_admin = conn.execute(
            "SELECT role FROM users WHERE id=3"
        ).fetchone()
        conn.close()
        self.assertEqual(target[0], "viewer")
        self.assertEqual(target[1], 1)
        self.assertEqual(target[2], json.dumps(["reports.view", "backup.manage"]))
        self.assertEqual(protected_admin[0], "admin")

    def test_admin_account_cannot_be_demoted_disabled_or_stripped_of_core_permissions(self):
        conn = sqlite3.connect(nexus.DB_PATH)
        conn.execute(
            """INSERT INTO users
               (id,username,full_name,role,active,permissions_json,password_hash,created_at)
               VALUES(2,'second.admin','مدير ثان','admin',1,?,'hash','2026-01-01')""",
            (json.dumps(["users.manage"]),),
        )
        conn.commit()
        conn.close()
        with self.client.session_transaction() as session:
            session.update(user_id=1, username="admin", full_name="مدير",
                           role="admin", permissions=["users.manage"])
        response = self.client.post("/users/2/edit", data={
            "username": "second.admin", "full_name": "مدير ثان", "role": "viewer",
            "active": "yes", "permissions": ["reports.view"],
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn("لا يمكن تغيير دور مدير النظام", response.get_data(as_text=True))
        conn = sqlite3.connect(nexus.DB_PATH)
        role, active, raw_permissions = conn.execute(
            "SELECT role,active,permissions_json FROM users WHERE id=2"
        ).fetchone()
        conn.close()
        self.assertEqual(role, "admin")
        self.assertEqual(active, 1)
        self.assertEqual(raw_permissions, json.dumps(["users.manage"]))

    def test_permission_changes_take_effect_on_the_next_request(self):
        with self.client.session_transaction() as session:
            session.update(user_id=1, username="admin", full_name="مدير",
                           role="admin", permissions=["users.manage"])
        conn = sqlite3.connect(nexus.DB_PATH)
        conn.execute(
            "UPDATE users SET role='viewer',permissions_json=? WHERE id=1",
            (json.dumps(["reports.view"]),),
        )
        conn.commit()
        conn.close()

        response = self.client.get("/users")
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as session:
            self.assertEqual(session["role"], "viewer")
            self.assertEqual(session["permissions"], ["reports.view"])

    def test_deactivated_user_session_is_cleared_on_the_next_request(self):
        with self.client.session_transaction() as session:
            session.update(user_id=1, username="admin", full_name="مدير",
                           role="admin", permissions=["users.manage"])
        conn = sqlite3.connect(nexus.DB_PATH)
        conn.execute("UPDATE users SET active=0 WHERE id=1")
        conn.commit()
        conn.close()

        response = self.client.get("/users")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])
        with self.client.session_transaction() as session:
            self.assertNotIn("user_id", session)

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
