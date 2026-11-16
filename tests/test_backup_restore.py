import json
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path

from backup_restore import (
    create_database_safety_copy,
    extract_validated_backup,
    restore_database_with_rollback,
)


def make_database(path, value):
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE users(id INTEGER PRIMARY KEY, username TEXT);
        CREATE TABLE accounts(id INTEGER PRIMARY KEY, code TEXT);
        CREATE TABLE journal_entries(id INTEGER PRIMARY KEY);
        CREATE TABLE journal_lines(id INTEGER PRIMARY KEY);
        CREATE TABLE sample(value TEXT);
    """)
    connection.execute("INSERT INTO sample(value) VALUES(?)", (value,))
    connection.commit()
    connection.close()


class BackupRestoreTests(unittest.TestCase):
    def test_extracts_and_validates_nexus_backup(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            database = root / "source.db"
            archive_path = root / "backup.zip"
            candidate = root / "candidate.db"
            make_database(database, "backup-data")
            with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.write(database, "nexus.db")
                archive.writestr("manifest.json", json.dumps({
                    "application": "NEXUS ERP",
                    "database_integrity": "ok",
                }))
            extract_validated_backup(archive_path, candidate)
            restored = sqlite3.connect(candidate)
            self.assertEqual(restored.execute("SELECT value FROM sample").fetchone()[0], "backup-data")
            restored.close()

    def test_rejects_zip_without_nexus_database(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            archive_path = root / "invalid.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("other.db", b"not a database")
            with self.assertRaisesRegex(ValueError, "nexus.db"):
                extract_validated_backup(archive_path, root / "candidate.db")

    def test_rejects_database_missing_required_nexus_tables(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            database = root / "wrong.db"
            connection = sqlite3.connect(database)
            connection.execute("CREATE TABLE sample(value TEXT)")
            connection.commit()
            connection.close()
            archive_path = root / "wrong.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.write(database, "nexus.db")
            with self.assertRaisesRegex(ValueError, "هيكل NEXUS"):
                extract_validated_backup(archive_path, root / "candidate.db")

    def test_restores_validated_database(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            live = root / "live.db"
            candidate = root / "candidate.db"
            safety = root / "safety.db"
            make_database(live, "old-data")
            make_database(candidate, "new-data")
            create_database_safety_copy(live, safety)
            restore_database_with_rollback(candidate, live, safety, lambda: None)
            connection = sqlite3.connect(live)
            self.assertEqual(connection.execute("SELECT value FROM sample").fetchone()[0], "new-data")
            connection.close()

    def test_rolls_back_original_database_when_initialization_fails(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            live = root / "live.db"
            candidate = root / "candidate.db"
            safety = root / "safety.db"
            make_database(live, "must-survive")
            make_database(candidate, "bad-restore")
            create_database_safety_copy(live, safety)

            def fail_initialization():
                raise RuntimeError("simulated schema initialization failure")

            with self.assertRaisesRegex(RuntimeError, "تمت إعادة قاعدة البيانات السابقة"):
                restore_database_with_rollback(candidate, live, safety, fail_initialization)
            connection = sqlite3.connect(live)
            self.assertEqual(connection.execute("SELECT value FROM sample").fetchone()[0], "must-survive")
            connection.close()


if __name__ == "__main__":
    unittest.main()
