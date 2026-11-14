import sqlite3
import unittest

from code_sequences import next_code


class NumberSequenceTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:", isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("CREATE TABLE customers(id INTEGER PRIMARY KEY, code TEXT UNIQUE)")
        self.db.executemany("INSERT INTO customers(code) VALUES(?)", [("CUS-000004",), ("CUS-000009",)])

    def tearDown(self):
        self.db.close()

    def test_sequence_continues_after_existing_legacy_codes(self):
        code = next_code(self.db, "customer", "CUS", "customers", "code")
        self.assertEqual(code, "CUS-000010")
        self.db.execute("INSERT INTO customers(code) VALUES(?)", (code,))
        self.db.commit()
        self.assertEqual(next_code(self.db, "customer", "CUS", "customers", "code"), "CUS-000011")

    def test_sequences_are_independent(self):
        self.db.execute("CREATE TABLE suppliers(id INTEGER PRIMARY KEY, code TEXT UNIQUE)")
        self.assertEqual(next_code(self.db, "customer", "CUS", "customers", "code"), "CUS-000010")
        self.db.commit()
        self.assertEqual(next_code(self.db, "supplier", "SUP", "suppliers", "code"), "SUP-000001")

    def test_failed_insert_can_rollback_reserved_number(self):
        code = next_code(self.db, "customer", "CUS", "customers", "code")
        self.db.rollback()
        self.assertEqual(next_code(self.db, "customer", "CUS", "customers", "code"), code)
