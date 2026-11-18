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

    def test_does_not_overwrite_existing_candidate_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            database = root / "source.db"
            archive_path = root / "backup.zip"
            candidate = root / "candidate.db"
            make_database(database, "backup-data")
            candidate.write_bytes(b"keep-this-file")
            with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.write(database, "nexus.db")

            with self.assertRaises(FileExistsError):
                extract_validated_backup(archive_path, candidate)

            self.assertEqual(candidate.read_bytes(), b"keep-this-file")

    def test_removes_extracted_candidate_when_database_integrity_fails(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            invalid = root / "invalid.db"
            connection = sqlite3.connect(invalid)
            connection.execute("CREATE TABLE unrelated(value TEXT)")
            connection.commit()
            connection.close()
            archive_path = root / "invalid.zip"
            candidate = root / "candidate.db"
            with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.write(invalid, "nexus.db")

            with self.assertRaisesRegex(ValueError, "هيكل NEXUS"):
                extract_validated_backup(archive_path, candidate)

            self.assertFalse(candidate.exists())

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

    def test_rejects_database_checksum_mismatch(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            database = root / "source.db"
            archive_path = root / "tampered.zip"
            make_database(database, "trusted-data")
            with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.write(database, "nexus.db")
                archive.writestr("manifest.json", json.dumps({
                    "application": "NEXUS ERP",
                    "database_integrity": "ok",
                    "database_sha256": "0" * 64,
                }))
            with self.assertRaisesRegex(ValueError, "بصمة قاعدة البيانات"):
                extract_validated_backup(archive_path, root / "candidate.db")
            self.assertFalse((root / "candidate.db").exists())

    def test_rejects_unexpected_archive_members(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            database = root / "source.db"
            archive_path = root / "extra.zip"
            make_database(database, "trusted-data")
            with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.write(database, "nexus.db")
                archive.writestr("unexpected.txt", "not part of a NEXUS backup")
            candidate = root / "candidate.db"
            with self.assertRaisesRegex(ValueError, "ملفات إضافية"):
                extract_validated_backup(archive_path, candidate)
            self.assertFalse(candidate.exists())

    def test_rejects_malformed_database_checksum(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            database = root / "source.db"
            archive_path = root / "bad-manifest.zip"
            make_database(database, "trusted-data")
            with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.write(database, "nexus.db")
                archive.writestr("manifest.json", json.dumps({
                    "application": "NEXUS ERP",
                    "database_sha256": "not-a-sha256",
                }))
            candidate = root / "candidate.db"
            with self.assertRaisesRegex(ValueError, "بصمة قاعدة البيانات داخل Manifest"):
                extract_validated_backup(archive_path, candidate)
            self.assertFalse(candidate.exists())

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
