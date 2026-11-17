import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import postgres_backup


class PostgreSQLBackupTests(unittest.TestCase):
    def _archive(self, folder, payload=b"custom-format-dump"):
        path = Path(folder) / "backup.zip"
        manifest = {
            "application": "NEXUS ERP",
            "backup_format": "postgresql-custom",
            "database_name": "nexus",
            "dump_sha256": hashlib.sha256(payload).hexdigest(),
        }
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("nexus.dump", payload)
            archive.writestr("manifest.json", json.dumps(manifest))
        return path

    def test_validates_manifest_checksum_and_custom_archive(self):
        with tempfile.TemporaryDirectory() as folder:
            archive = self._archive(folder)
            with patch.object(postgres_backup, "_run") as run:
                manifest = postgres_backup.validate_postgres_backup_archive(
                    archive, pg_restore_path="pg_restore"
                )
            self.assertEqual(manifest["database_name"], "nexus")
            self.assertEqual(run.call_args.args[0][:2], ["pg_restore", "--list"])

    def test_rejects_checksum_mismatch_before_pg_restore(self):
        with tempfile.TemporaryDirectory() as folder:
            archive = self._archive(folder, payload=b"tampered")
            with zipfile.ZipFile(archive, "a") as zipped:
                manifest = json.loads(zipped.read("manifest.json"))
                manifest["dump_sha256"] = "0" * 64
                zipped.writestr("manifest.json", json.dumps(manifest))
            with patch.object(postgres_backup, "_run") as run:
                with self.assertRaisesRegex(ValueError, "checksum"):
                    postgres_backup.validate_postgres_backup_archive(
                        archive, pg_restore_path="pg_restore"
                    )
                run.assert_not_called()

    def test_rejects_archive_with_unexpected_members(self):
        with tempfile.TemporaryDirectory() as folder:
            archive = self._archive(folder)
            with zipfile.ZipFile(archive, "a") as zipped:
                zipped.writestr("../outside.txt", "no")
            with patch.object(postgres_backup, "_run"):
                with self.assertRaisesRegex(ValueError, "exactly"):
                    postgres_backup.validate_postgres_backup_archive(
                        archive, pg_restore_path="pg_restore"
                    )

    def test_restore_database_name_is_validated_before_database_creation(self):
        with tempfile.TemporaryDirectory() as folder:
            archive = self._archive(folder)
            with patch.object(postgres_backup, "_run"):
                with self.assertRaisesRegex(ValueError, "database name"):
                    postgres_backup.restore_postgres_backup_to_new_database(
                        archive, "dbname=nexus", database_name="Bad-Name",
                        pg_restore_path="pg_restore",
                    )


if __name__ == "__main__":
    unittest.main()
