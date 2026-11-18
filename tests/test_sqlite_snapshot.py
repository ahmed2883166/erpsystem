import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from scripts.snapshot_sqlite_before_postgres import (
    create_sqlite_snapshot,
    verify_sqlite_snapshot,
)


class SQLiteSnapshotTests(unittest.TestCase):
    def _source(self, folder):
        path = Path(folder) / "nexus.db"
        connection = sqlite3.connect(path)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.executescript("""
                CREATE TABLE accounts(
                    id INTEGER PRIMARY KEY,
                    parent_id INTEGER REFERENCES accounts(id),
                    name TEXT NOT NULL
                );
                CREATE TABLE journal_entries(
                    id INTEGER PRIMARY KEY,
                    reference TEXT NOT NULL UNIQUE
                );
                CREATE TABLE journal_lines(
                    id INTEGER PRIMARY KEY,
                    entry_id INTEGER NOT NULL REFERENCES journal_entries(id),
                    debit TEXT NOT NULL,
                    credit TEXT NOT NULL
                );
            """)
            connection.executemany(
                "INSERT INTO accounts(id,parent_id,name) VALUES(?,?,?)",
                [(1, None, "Root"), (2, 1, "Cash")],
            )
            connection.execute(
                "INSERT INTO journal_entries(id,reference) VALUES(1,'OPEN-001')"
            )
            connection.executemany(
                "INSERT INTO journal_lines(id,entry_id,debit,credit) VALUES(?,?,?,?)",
                [(1, 1, "100.25", "0"), (2, 1, "0", "100.25")],
            )
            connection.commit()
        finally:
            connection.close()
        return path

    def test_snapshot_uses_online_backup_and_verifies_checksum_and_rows(self):
        with tempfile.TemporaryDirectory(prefix="admrail-snapshot-test-") as folder:
            source = self._source(folder)
            original_bytes = source.read_bytes()
            result = create_sqlite_snapshot(
                source,
                Path(folder) / "backups",
                created_at=datetime(2026, 11, 18, tzinfo=timezone.utc),
            )
            self.assertTrue(result["verified"])
            self.assertEqual(result["table_count"], 3)
            self.assertEqual(result["row_count_total"], 5)
            self.assertEqual(source.read_bytes(), original_bytes)
            verified = verify_sqlite_snapshot(result["snapshot"], result["manifest"])
            self.assertTrue(verified["verified"])
            self.assertEqual(verified["sha256"], result["sha256"])

    def test_snapshot_refuses_to_overwrite_existing_backup(self):
        with tempfile.TemporaryDirectory(prefix="admrail-snapshot-overwrite-") as folder:
            source = self._source(folder)
            backups = Path(folder) / "backups"
            moment = datetime(2026, 11, 18, tzinfo=timezone.utc)
            create_sqlite_snapshot(source, backups, created_at=moment)
            with self.assertRaisesRegex(FileExistsError, "refusing to overwrite"):
                create_sqlite_snapshot(source, backups, created_at=moment)

    def test_verifier_rejects_modified_snapshot(self):
        with tempfile.TemporaryDirectory(prefix="admrail-snapshot-corrupt-") as folder:
            source = self._source(folder)
            result = create_sqlite_snapshot(source, Path(folder) / "backups")
            snapshot = Path(result["snapshot"])
            with snapshot.open("ab") as handle:
                handle.write(b"tampered")
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                verify_sqlite_snapshot(snapshot, result["manifest"])

    def test_snapshot_refuses_missing_source_without_creating_database(self):
        with tempfile.TemporaryDirectory(prefix="admrail-snapshot-missing-") as folder:
            missing = Path(folder) / "missing.sqlite3"
            with self.assertRaises(FileNotFoundError):
                create_sqlite_snapshot(missing, Path(folder) / "backups")
            self.assertFalse(missing.exists())


if __name__ == "__main__":
    unittest.main()
