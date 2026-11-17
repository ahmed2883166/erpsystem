"""Tests for the opt-in PostgreSQL migration runner.

Integration tests use unique disposable schemas only. They never touch the
application schema or a company database.
"""
import hashlib
import os
import tempfile
import unittest
import uuid
from pathlib import Path

from postgres_database import connect_postgres
from postgres_migrations import Migration, apply_migrations, load_migrations, quote_schema_name

POSTGRES_DSN = os.environ.get("NEXUS_TEST_POSTGRES_DSN", "").strip()


class PostgreSQLMigrationUnitTests(unittest.TestCase):
    def test_schema_identifier_is_validated_and_quoted(self):
        self.assertEqual(quote_schema_name("nexus_test"), '"nexus_test"')
        for value in ("", "nexus.test", "nexus-test", 'nexus"; DROP SCHEMA public;--', "x" * 64):
            with self.subTest(value=value), self.assertRaises(ValueError):
                quote_schema_name(value)

    def test_loads_migrations_in_version_order_with_sha256(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "0002_add_second.sql").write_text(
                "CREATE TABLE second_table (id BIGINT);", encoding="utf-8"
            )
            (root / "0001_create_first.sql").write_text(
                "CREATE TABLE first_table (id BIGINT);", encoding="utf-8"
            )
            migrations = load_migrations(root)
        self.assertEqual([item.version for item in migrations], [1, 2])
        self.assertEqual(migrations[0].name, "0001_create_first.sql")
        self.assertEqual(len(migrations[0].checksum), 64)

    def test_rejects_invalid_or_empty_migration_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "README.txt").write_text("not a migration", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Invalid PostgreSQL migration filename"):
                load_migrations(root)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "0001_empty.sql").write_text("  ", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "cannot be empty"):
                load_migrations(root)

    def test_rejects_migration_checksum_mismatch_before_execution(self):
        class Connection:
            autocommit = False
            info = None

            def execute(self, *_args):
                raise AssertionError("No SQL should execute for an invalid migration.")

        sql = 'CREATE TABLE "probe" (id BIGINT)'
        migration = Migration(
            version=1,
            name="0001_probe.sql",
            sql=sql,
            checksum=hashlib.sha256(b"different SQL").hexdigest(),
        )
        with self.assertRaisesRegex(ValueError, "checksum does not match"):
            apply_migrations(Connection(), [migration], schema="nexus_test")

    def test_rejects_migration_name_version_mismatch_before_execution(self):
        class Connection:
            autocommit = False
            info = None

            def execute(self, *_args):
                raise AssertionError("No SQL should execute for an invalid migration.")

        sql = 'CREATE TABLE "probe" (id BIGINT)'
        migration = Migration(
            version=2,
            name="0001_probe.sql",
            sql=sql,
            checksum=hashlib.sha256(sql.encode("utf-8")).hexdigest(),
        )
        with self.assertRaisesRegex(ValueError, "does not match version"):
            apply_migrations(Connection(), [migration], schema="nexus_test")

    def test_rejects_autocommit_connections_before_execution(self):
        class Connection:
            autocommit = True
            info = None

        with self.assertRaisesRegex(ValueError, "autocommit=False"):
            apply_migrations(Connection(), [], schema="nexus_test")


@unittest.skipUnless(POSTGRES_DSN, "NEXUS_TEST_POSTGRES_DSN is not configured")
class PostgreSQLMigrationIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.connection = connect_postgres(POSTGRES_DSN)
        self.schema = "nexus_mig_" + uuid.uuid4().hex[:12]

    def tearDown(self):
        if getattr(self, "connection", None):
            self.connection.rollback()
            self.connection.execute(
                "DROP SCHEMA IF EXISTS " + quote_schema_name(self.schema) + " CASCADE"
            )
            self.connection.commit()
            self.connection.close()

    @staticmethod
    def _migration(version, name, sql):
        return Migration(
            version=version,
            name=name,
            sql=sql,
            checksum=hashlib.sha256(sql.encode("utf-8")).hexdigest(),
        )

    def test_applies_once_and_is_idempotent_in_target_schema(self):
        migrations = [
            self._migration(1, "0001_create_probe.sql",
                            'CREATE TABLE "probe" (id BIGINT PRIMARY KEY)')
        ]
        self.assertEqual(apply_migrations(self.connection, migrations, schema=self.schema), [1])
        self.assertEqual(apply_migrations(self.connection, migrations, schema=self.schema), [])
        row = self.connection.execute(
            "SELECT COUNT(*) AS count FROM "
            + quote_schema_name(self.schema)
            + '."schema_migrations"'
        ).fetchone()
        self.assertEqual(row["count"], 1)
        table = self.connection.execute(
            "SELECT to_regclass(%s) AS table_name",
            (self.schema + ".probe",),
        ).fetchone()
        self.assertIsNotNone(table["table_name"])

    def test_executes_multi_statement_migration_with_dollar_quoted_block(self):
        migrations = [
            self._migration(
                1,
                "0001_multi_statement.sql",
                """
                CREATE TABLE "multi_probe" (id BIGINT PRIMARY KEY, label TEXT);
                INSERT INTO "multi_probe"(id, label) VALUES (1, 'first; value');
                DO $nexus$
                BEGIN
                    UPDATE "multi_probe" SET label = 'done; value' WHERE id = 1;
                END
                $nexus$;
                """,
            )
        ]
        self.assertEqual(apply_migrations(self.connection, migrations, schema=self.schema), [1])
        row = self.connection.execute(
            'SELECT label FROM ' + quote_schema_name(self.schema) + '."multi_probe" WHERE id=1'
        ).fetchone()
        self.assertEqual(row["label"], "done; value")

    def test_rejects_migration_set_missing_previously_applied_versions(self):
        original = self._migration(1, "0001_probe.sql", 'CREATE TABLE "probe" (id BIGINT)')
        apply_migrations(self.connection, [original], schema=self.schema)
        with self.assertRaisesRegex(RuntimeError, "missing from the supplied migration set: 1"):
            apply_migrations(self.connection, [], schema=self.schema)

    def test_modified_applied_migration_is_rejected(self):
        original = self._migration(1, "0001_probe.sql", 'CREATE TABLE "probe" (id BIGINT)')
        apply_migrations(self.connection, [original], schema=self.schema)
        changed = self._migration(
            1, "0001_probe.sql", 'CREATE TABLE "probe" (id BIGINT, label TEXT)'
        )
        with self.assertRaisesRegex(RuntimeError, "was modified"):
            apply_migrations(self.connection, [changed], schema=self.schema)

    def test_failed_batch_rolls_back_schema_and_migration_records(self):
        migrations = [
            self._migration(1, "0001_ok.sql", 'CREATE TABLE "first_probe" (id BIGINT)'),
            self._migration(2, "0002_fail.sql", 'CREATE TABLE "broken_probe" ('),
        ]
        with self.assertRaises(Exception):
            apply_migrations(self.connection, migrations, schema=self.schema)
        exists = self.connection.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = %s) AS present",
            (self.schema,),
        ).fetchone()["present"]
        self.assertFalse(exists)


if __name__ == "__main__":
    unittest.main()
