import hashlib
import json
import os
import shutil
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
            with zipfile.ZipFile(archive, "r") as original:
                payload = original.read("nexus.dump")
                manifest = json.loads(original.read("manifest.json"))
            manifest["dump_sha256"] = "0" * 64
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as corrupted:
                corrupted.writestr("nexus.dump", payload)
                corrupted.writestr("manifest.json", json.dumps(manifest))
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


class PostgreSQLBackupRoundTripIntegrationTests(unittest.TestCase):
    """Exercise real pg_dump/pg_restore against disposable PostgreSQL databases."""

    @unittest.skipUnless(
        os.environ.get("NEXUS_TEST_POSTGRES_DSN")
        and shutil.which("pg_dump")
        and shutil.which("pg_restore"),
        "PostgreSQL server and native client tools are required",
    )
    def test_real_backup_restore_round_trip_and_existing_database_protection(self):
        import uuid

        import psycopg
        from psycopg import sql
        from psycopg.conninfo import make_conninfo

        admin_dsn = os.environ["NEXUS_TEST_POSTGRES_DSN"]
        suffix = uuid.uuid4().hex[:12]
        source_db = "nexus_bk_src_" + suffix
        restored_db = "nexus_bk_dst_" + suffix
        with tempfile.TemporaryDirectory(prefix="nexus-pg-roundtrip-") as folder:
            try:
                with psycopg.connect(admin_dsn, autocommit=True) as admin:
                    admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(source_db)))

                source_dsn = make_conninfo(admin_dsn, dbname=source_db)
                with psycopg.connect(source_dsn) as source:
                    source.execute("CREATE TABLE backup_probe(id BIGINT PRIMARY KEY, payload TEXT NOT NULL)")
                    source.execute(
                        "INSERT INTO backup_probe(id,payload) VALUES(%s,%s)",
                        (1, "NEXUS PostgreSQL round-trip"),
                    )

                archive = postgres_backup.create_postgres_backup_archive(
                    source_dsn, folder, keep_count=5
                )
                self.assertTrue(archive.is_file())
                checked = postgres_backup.validate_postgres_backup_archive(archive)
                self.assertEqual(checked["database_name"], source_db)

                restored = postgres_backup.restore_postgres_backup_to_new_database(
                    archive, admin_dsn, database_name=restored_db
                )
                self.assertTrue(restored["restored"])
                self.assertEqual(restored["source_database_name"], source_db)
                restored_dsn = make_conninfo(admin_dsn, dbname=restored_db)
                with psycopg.connect(restored_dsn) as restored_connection:
                    row = restored_connection.execute(
                        "SELECT id,payload FROM backup_probe"
                    ).fetchone()
                    self.assertEqual(row, (1, "NEXUS PostgreSQL round-trip"))

                with self.assertRaises(FileExistsError):
                    postgres_backup.restore_postgres_backup_to_new_database(
                        archive, admin_dsn, database_name=source_db
                    )
                with psycopg.connect(source_dsn) as source:
                    row = source.execute("SELECT payload FROM backup_probe WHERE id=1").fetchone()
                    self.assertEqual(row, ("NEXUS PostgreSQL round-trip",))
            finally:
                with psycopg.connect(admin_dsn, autocommit=True) as admin:
                    for database in (restored_db, source_db):
                        admin.execute(
                            sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database))
                        )
