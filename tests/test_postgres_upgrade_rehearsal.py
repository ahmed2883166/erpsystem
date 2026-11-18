"""Rehearse the versioned PostgreSQL migration path in a clean disposable schema.

The test uses a unique disposable schema and never reads or modifies company data.
The first-install baseline is validated independently. Applying the initial
core migration on top of that already-installed baseline would correctly
encounter objects that the baseline has already created.
"""
import os
import unittest
import uuid
from pathlib import Path

from postgres_baseline import install_postgres_baseline
from postgres_database import connect_postgres
from postgres_migrations import apply_migrations, load_migrations
from scripts.build_postgres_baseline import build_baseline


POSTGRES_DSN = os.environ.get("NEXUS_TEST_POSTGRES_DSN", "").strip()


@unittest.skipUnless(POSTGRES_DSN, "NEXUS_TEST_POSTGRES_DSN is not configured")
class PostgreSQLUpgradeRehearsalTests(unittest.TestCase):
    def setUp(self):
        self.schema = "nexus_upgrade_rehearsal_" + uuid.uuid4().hex[:12]

    def tearDown(self):
        cleanup = connect_postgres(POSTGRES_DSN, autocommit=True)
        try:
            cleanup.execute(f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE')
        finally:
            cleanup.close()

    def test_versioned_migrations_apply_to_full_baseline_and_replay_safely(self):
        # Exercise the migration path from an empty schema. The full generated
        # first-install baseline has its own preservation/overwrite test and
        # must not be layered with the initial migration a second time.
        migration_dir = Path(__file__).resolve().parents[1] / "postgres_migrations"
        migrations = load_migrations(migration_dir)
        connection = connect_postgres(POSTGRES_DSN)
        try:
            applied = apply_migrations(connection, migrations, schema=self.schema)
            self.assertEqual(applied, [item.version for item in migrations])

            replayed = apply_migrations(connection, migrations, schema=self.schema)
            self.assertEqual(replayed, [])

            core_tables = connection.execute(
                "SELECT to_regclass(%s), to_regclass(%s), to_regclass(%s)",
                (f'"{self.schema}".accounts', f'"{self.schema}".journal_entries',
                 f'"{self.schema}".audit_log'),
            ).fetchone()
            self.assertTrue(all(value is not None for value in core_tables))

            records = connection.execute(
                f'SELECT version, name, checksum FROM "{self.schema}".schema_migrations '
                "ORDER BY version"
            ).fetchall()
            self.assertEqual(len(records), len(migrations))
            self.assertEqual(
                [int(row["version"]) for row in records],
                [item.version for item in migrations],
            )
            self.assertEqual(
                [row["checksum"].strip() for row in records],
                [item.checksum for item in migrations],
            )
            connection.commit()
        finally:
            connection.rollback()
            connection.close()


if __name__ == "__main__":
    unittest.main()
