import sqlite3
import unittest

from security_controls import login_attempts_exceeded, linked_operational_source


class SecurityControlTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("""CREATE TABLE login_attempts(
            id INTEGER PRIMARY KEY, username TEXT NOT NULL,
            remote_addr TEXT NOT NULL, attempted_at TEXT NOT NULL
        )""")

    def tearDown(self):
        self.conn.close()

    def test_username_and_ip_limit_is_enforced(self):
        self.conn.executemany(
            "INSERT INTO login_attempts(username,remote_addr,attempted_at) VALUES(?,?,?)",
            [("alice", "10.0.0.1", "2026-11-11T10:00:00")] * 5,
        )
        self.assertTrue(login_attempts_exceeded(
            self.conn, "alice", "10.0.0.1", "2026-11-11T09:45:00"
        ))

    def test_ip_limit_counts_attempts_across_different_usernames(self):
        self.conn.executemany(
            "INSERT INTO login_attempts(username,remote_addr,attempted_at) VALUES(?,?,?)",
            [(f"user{i}", "10.0.0.2", "2026-11-11T10:00:00") for i in range(20)],
        )
        self.assertTrue(login_attempts_exceeded(
            self.conn, "another-user", "10.0.0.2", "2026-11-11T09:45:00"
        ))

    def test_attempts_outside_window_do_not_count(self):
        self.conn.execute(
            "INSERT INTO login_attempts(username,remote_addr,attempted_at) VALUES(?,?,?)",
            ("alice", "10.0.0.3", "2026-11-11T09:00:00"),
        )
        self.assertFalse(login_attempts_exceeded(
            self.conn, "alice", "10.0.0.3", "2026-11-11T09:45:00"
        ))

    def test_journal_linked_to_operational_document_is_detected(self):
        self.conn.execute("CREATE TABLE journal_entries(id INTEGER PRIMARY KEY)")
        self.conn.execute("CREATE TABLE supplier_bills(id INTEGER PRIMARY KEY, journal_entry_id INTEGER)")
        self.conn.execute("INSERT INTO supplier_bills VALUES(1, 77)")
        self.assertEqual(linked_operational_source(self.conn, 77), "supplier_bills")

    def test_unlinked_manual_journal_can_be_reversed(self):
        self.conn.execute("CREATE TABLE journal_entries(id INTEGER PRIMARY KEY)")
        self.conn.execute("CREATE TABLE journal_lines(id INTEGER PRIMARY KEY, entry_id INTEGER)")
        self.conn.execute("CREATE TABLE supplier_bills(id INTEGER PRIMARY KEY, journal_entry_id INTEGER)")
        self.conn.execute("INSERT INTO supplier_bills VALUES(1, NULL)")
        self.assertIsNone(linked_operational_source(self.conn, 77))


if __name__ == "__main__":
    unittest.main()
