import sqlite3
import unittest

from sqlite_accounting_migrations import (migrate_journal_line_amounts_to_text,
                                         migrate_currency_rates_to_text)
from db_compat import execute_script, transactional_migration


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
            CREATE TABLE currencies(
                code TEXT PRIMARY KEY, name_ar TEXT NOT NULL,
                rate_to_base REAL NOT NULL DEFAULT 0, updated_at TEXT
            );
            CREATE INDEX idx_currencies_updated ON currencies(updated_at);
            INSERT INTO currencies(code,name_ar,rate_to_base,updated_at)
                VALUES('USD','دولار',30.125,'2026-01-01');
            CREATE TABLE journal_lines (
                id INTEGER PRIMARY KEY,
                entry_id INTEGER NOT NULL REFERENCES journal_entries(id) ON DELETE CASCADE,
                account_id INTEGER NOT NULL REFERENCES accounts(id),
                debit REAL NOT NULL DEFAULT 0,
                credit REAL NOT NULL DEFAULT 0,
                memo TEXT,
                branch_id INTEGER,
                cost_center_id INTEGER
            );
            CREATE INDEX idx_journal_lines_entry ON journal_lines(entry_id);
            CREATE INDEX idx_journal_lines_dimensions ON journal_lines(branch_id,cost_center_id);
            CREATE TRIGGER journal_lines_guard_insert
            BEFORE INSERT ON journal_lines
            WHEN NEW.debit < 0
            BEGIN SELECT RAISE(ABORT, 'negative debit'); END;
            INSERT INTO accounts(id,code) VALUES(4,'1000');
            INSERT INTO journal_entries(id,entry_date) VALUES(9,'2026-01-01');
            INSERT INTO journal_lines(id,entry_id,account_id,debit,credit,memo,branch_id,cost_center_id)
                VALUES(15,9,4,123.45,0,'legacy line',3,8);
        """)
        self.connection.commit()

    def tearDown(self):
        self.connection.close()

    def test_migration_preserves_ids_values_and_foreign_keys(self):
        self.assertTrue(migrate_journal_line_amounts_to_text(self.connection))
        row = self.connection.execute(
            "SELECT id,entry_id,account_id,debit,credit,memo,branch_id,cost_center_id FROM journal_lines WHERE id=15"
        ).fetchone()
        self.assertEqual(row, (15, 9, 4, "123.45", "0.0", "legacy line", 3, 8))
        columns = {
            item[1]: item[2].upper()
            for item in self.connection.execute("PRAGMA table_info(journal_lines)")
        }
        self.assertEqual(columns["debit"], "TEXT")
        self.assertEqual(columns["credit"], "TEXT")
        self.assertEqual(columns["branch_id"], "INTEGER")
        self.assertEqual(columns["cost_center_id"], "INTEGER")
        indexes = {
            row[0] for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='journal_lines'"
            )
        }
        self.assertIn("idx_journal_lines_entry", indexes)
        self.assertIn("idx_journal_lines_dimensions", indexes)
        trigger = self.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='journal_lines_guard_insert'"
        ).fetchone()
        self.assertIsNotNone(trigger)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "negative debit"):
            self.connection.execute(
                "INSERT INTO journal_lines(id,entry_id,account_id,debit,credit,memo,branch_id,cost_center_id) "
                "VALUES(16,9,4,-1,0,'bad',3,8)"
            )
        self.assertEqual(
            self.connection.execute("PRAGMA foreign_key_check").fetchall(), []
        )

    def test_currency_rate_migration_preserves_values_columns_and_indexes(self):
        self.assertTrue(migrate_currency_rates_to_text(self.connection))
        row = self.connection.execute(
            "SELECT code,name_ar,rate_to_base,updated_at FROM currencies WHERE code='USD'"
        ).fetchone()
        self.assertEqual(row, ("USD", "دولار", "30.125", "2026-01-01"))
        columns = {
            item[1]: item[2].upper()
            for item in self.connection.execute("PRAGMA table_info(currencies)")
        }
        self.assertEqual(columns["rate_to_base"], "TEXT")
        indexes = {
            row[0] for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='currencies'"
            )
        }
        self.assertIn("idx_currencies_updated", indexes)

    def test_migration_is_noop_after_decimal_storage_is_established(self):
        migrate_journal_line_amounts_to_text(self.connection)
        self.assertFalse(migrate_journal_line_amounts_to_text(self.connection))
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM journal_lines").fetchone()[0],
            1,
        )

    def test_interrupted_legacy_scratch_table_is_not_reused_or_duplicated(self):
        self.connection.execute("""
            CREATE TABLE journal_lines_decimal_new (
                id INTEGER PRIMARY KEY, entry_id INTEGER, account_id INTEGER,
                debit TEXT, credit TEXT, memo TEXT, branch_id INTEGER,
                cost_center_id INTEGER
            )
        """)
        self.connection.execute("""
            INSERT INTO journal_lines_decimal_new
            VALUES(15,9,4,'123.45','0','stale copied row',3,8)
        """)
        self.connection.commit()
        self.assertTrue(migrate_journal_line_amounts_to_text(self.connection))
        rows = self.connection.execute(
            "SELECT id,memo FROM journal_lines ORDER BY id"
        ).fetchall()
        self.assertEqual(rows, [(15, "legacy line")])

    def test_execute_script_failure_rolls_back_every_statement_in_migration(self):
        @transactional_migration
        def broken_migration(connection):
            execute_script(connection, """
                CREATE TABLE migration_first (id INTEGER PRIMARY KEY);
                CREATE TABLE migration_second (id INTEGER PRIMARY KEY);
                THIS IS NOT VALID SQL;
            """)

        with self.assertRaises(sqlite3.OperationalError):
            broken_migration(self.connection)
        remaining = self.connection.execute("""
            SELECT name FROM sqlite_master
            WHERE type='table' AND name IN ('migration_first','migration_second')
        """).fetchall()
        self.assertEqual(remaining, [])


if __name__ == "__main__":
    unittest.main()
