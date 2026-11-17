import sqlite3
import unittest

from sqlite_accounting_migrations import migrate_journal_line_amounts_to_text


class JournalLineAmountMigrationTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.executescript("""
            CREATE TABLE accounts (
                id INTEGER PRIMARY KEY, code TEXT NOT NULL
            );
            CREATE TABLE journal_entries (
                id INTEGER PRIMARY KEY, entry_date TEXT NOT NULL
            );
            CREATE TABLE journal_lines (
                id INTEGER PRIMARY KEY,
                entry_id INTEGER NOT NULL REFERENCES journal_entries(id) ON DELETE CASCADE,
                account_id INTEGER NOT NULL REFERENCES accounts(id),
                debit REAL NOT NULL DEFAULT 0,
                credit REAL NOT NULL DEFAULT 0,
                memo TEXT
            );
            CREATE INDEX idx_journal_lines_entry ON journal_lines(entry_id);
            INSERT INTO accounts(id,code) VALUES(4,'1000');
            INSERT INTO journal_entries(id,entry_date) VALUES(9,'2026-01-01');
            INSERT INTO journal_lines(id,entry_id,account_id,debit,credit,memo)
                VALUES(15,9,4,123.45,0,'legacy line');
        """)

    def tearDown(self):
        self.connection.close()

    def test_migration_preserves_ids_values_and_foreign_keys(self):
        self.assertTrue(migrate_journal_line_amounts_to_text(self.connection))
        row = self.connection.execute(
            "SELECT id,entry_id,account_id,debit,credit,memo FROM journal_lines WHERE id=15"
        ).fetchone()
        self.assertEqual(row, (15, 9, 4, "123.45", "0.0", "legacy line"))
        columns = {
            item[1]: item[2].upper()
            for item in self.connection.execute("PRAGMA table_info(journal_lines)")
        }
        self.assertEqual(columns["debit"], "TEXT")
        self.assertEqual(columns["credit"], "TEXT")
        self.assertEqual(
            self.connection.execute("PRAGMA foreign_key_check").fetchall(), []
        )

    def test_migration_is_noop_after_decimal_storage_is_established(self):
        migrate_journal_line_amounts_to_text(self.connection)
        self.assertFalse(migrate_journal_line_amounts_to_text(self.connection))
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM journal_lines").fetchone()[0],
            1,
        )


if __name__ == "__main__":
    unittest.main()
