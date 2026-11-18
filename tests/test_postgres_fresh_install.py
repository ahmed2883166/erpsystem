"""End-to-end first-install acceptance for a brand-new NEXUS PostgreSQL schema.

This builds the schema from a disposable SQLite initialization, installs it into
an isolated PostgreSQL schema, and proves the install is recorded and empty.
No company database or company records are read or modified.
"""
import hashlib
import os
import unittest
import uuid

from postgres_baseline import install_postgres_baseline
from postgres_database import connect_postgres
from scripts.build_postgres_baseline import build_baseline


POSTGRES_DSN = os.environ.get("NEXUS_TEST_POSTGRES_DSN", "").strip()


@unittest.skipUnless(POSTGRES_DSN, "NEXUS_TEST_POSTGRES_DSN is not configured")
class PostgreSQLFreshInstallAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.schema = "nexus_fresh_install_" + uuid.uuid4().hex[:12]
        self.connection = connect_postgres(POSTGRES_DSN, autocommit=True)

    def tearDown(self):
        try:
            self.connection.execute(
                f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE'
            )
        finally:
            self.connection.close()

    def test_full_generated_schema_installs_atomically_and_starts_empty(self):
        ddl, review = build_baseline()
        digest = install_postgres_baseline(
            self.connection, ddl, schema=self.schema
        )

        self.assertEqual(digest, hashlib.sha256(ddl.encode("utf-8")).hexdigest())
        installed = self.connection.execute(
            f'SELECT baseline_sha256 FROM "{self.schema}".nexus_schema_installation WHERE id=1'
        ).fetchone()
        self.assertEqual(installed["baseline_sha256"].strip(), digest)

        counts = self.connection.execute(
            """SELECT
                 (SELECT COUNT(*) FROM information_schema.tables
                   WHERE table_schema=%s AND table_type='BASE TABLE') AS tables,
                 (SELECT COUNT(*) FROM information_schema.columns
                   WHERE table_schema=%s) AS columns,
                 (SELECT COUNT(*) FROM information_schema.table_constraints
                   WHERE constraint_schema=%s AND constraint_type='FOREIGN KEY') AS foreign_keys,
                 (SELECT COUNT(*) FROM pg_trigger t
                   JOIN pg_class c ON c.oid=t.tgrelid
                   JOIN pg_namespace n ON n.oid=c.relnamespace
                   WHERE n.nspname=%s AND NOT t.tgisinternal) AS triggers""",
            (self.schema, self.schema, self.schema, self.schema),
        ).fetchone()

        self.assertEqual(counts["tables"], review["table_count"] + 1)
        self.assertEqual(counts["columns"], review["column_count"] + 3)
        self.assertEqual(counts["foreign_keys"], review["foreign_key_count"])
        self.assertEqual(counts["triggers"], review["trigger_count"])

        tables = self.connection.execute(
            """SELECT table_name FROM information_schema.tables
               WHERE table_schema=%s AND table_type='BASE TABLE'
                 AND table_name<>'nexus_schema_installation'
               ORDER BY table_name""",
            (self.schema,),
        ).fetchall()
        self.assertEqual(len(tables), review["table_count"])
        for row in tables:
            table_name = row["table_name"].replace('"', '""')
            count = self.connection.execute(
                f'SELECT COUNT(*) AS n FROM "{self.schema}"."{table_name}"'
            ).fetchone()["n"]
            self.assertEqual(count, 0, f"Fresh-install table {row['table_name']} is not empty")

    def test_full_install_refuses_nonempty_schema_without_removing_existing_objects(self):
        self.connection.execute(f'CREATE SCHEMA "{self.schema}"')
        self.connection.execute(
            f'CREATE TABLE "{self.schema}".keep_me (id BIGINT PRIMARY KEY)'
        )
        ddl, _ = build_baseline()
        with self.assertRaisesRegex(RuntimeError, "is not empty"):
            install_postgres_baseline(self.connection, ddl, schema=self.schema)

        kept = self.connection.execute(
            "SELECT to_regclass(%s) AS relation",
            (self.schema + ".keep_me",),
        ).fetchone()["relation"]
        self.assertIsNotNone(kept)
        unexpected = self.connection.execute(
            "SELECT to_regclass(%s) AS relation",
            (self.schema + ".accounts",),
        ).fetchone()["relation"]
        self.assertIsNone(unexpected)


if __name__ == "__main__":
    unittest.main()
