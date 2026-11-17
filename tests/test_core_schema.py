import sqlite3
import unittest

from core_schema import initialize_core_schema
from db_compat import table_columns, table_names


class CoreSchemaTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.execute("PRAGMA foreign_keys = ON")

    def tearDown(self):
        self.connection.close()

    def test_initializes_all_core_tables_and_expected_columns(self):
        self.assertTrue(initialize_core_schema(self.connection))
        expected = {
            "users", "accounts", "journal_entries", "journal_lines",
            "audit_log", "company_settings", "currencies", "tax_rates",
            "login_attempts", "accounting_periods",
        }
        self.assertTrue(expected.issubset(table_names(self.connection)))
        self.assertTrue({"username", "password_hash", "must_change_password"}.issubset(
            table_columns(self.connection, "users")
        ))
        self.assertTrue({"entry_id", "account_id", "debit", "credit"}.issubset(
            table_columns(self.connection, "journal_lines")
        ))

    def test_initialization_is_idempotent_and_preserves_existing_rows(self):
        initialize_core_schema(self.connection)
        self.connection.execute(
            "INSERT INTO users(username,full_name,password_hash,role,created_at) "
            "VALUES('existing','Existing User','hash','admin','2026-01-01')"
        )
        self.connection.commit()

        self.assertTrue(initialize_core_schema(self.connection))
        row = self.connection.execute(
            "SELECT username,password_hash FROM users WHERE username='existing'"
        ).fetchone()
        self.assertEqual(row, ("existing", "hash"))

    def test_core_journal_foreign_keys_remain_enforced(self):
        initialize_core_schema(self.connection)
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "INSERT INTO journal_lines(entry_id,account_id,debit,credit) VALUES(1,1,'10','0')"
            )


if __name__ == "__main__":
    unittest.main()
