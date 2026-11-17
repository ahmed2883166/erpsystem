"""Install the generated full NEXUS PostgreSQL schema into a fresh schema.

This is a first-install path for a new company database, not a SQLite data
migration. It refuses non-empty schemas and records the exact baseline hash.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from db_compat import _split_sql_statements
from postgres_migrations import quote_schema_name

_LOCK_NAMESPACE = 1313162835
_LOCK_ID = 2


def install_postgres_baseline(connection, baseline_sql: str, *, schema: str = "nexus") -> str:
    """Atomically install a generated full-schema SQL baseline into an empty schema.

    The connection must use autocommit=True so the installer controls the
    complete DDL transaction. No existing tables, views, sequences, or data are
    dropped or overwritten.
    """
    if getattr(connection, "autocommit", None) is not True:
        raise ValueError("Baseline installation requires an autocommit=True connection.")
    if not isinstance(baseline_sql, str) or not baseline_sql.strip():
        raise ValueError("PostgreSQL baseline SQL is required.")

    quoted_schema = quote_schema_name(schema)
    statements = _split_sql_statements(baseline_sql)
    if not statements:
        raise ValueError("PostgreSQL baseline contains no SQL statements.")
    normalized = [item.strip().rstrip(";").strip().upper() for item in statements]
    if normalized[0] != "BEGIN" or normalized[-1] != "COMMIT":
        raise ValueError("Generated PostgreSQL baseline must be wrapped in BEGIN/COMMIT.")
    ddl = statements[1:-1]
    if not ddl:
        raise ValueError("PostgreSQL baseline contains no schema DDL.")
    digest = hashlib.sha256(baseline_sql.encode("utf-8")).hexdigest()

    connection.execute("SELECT pg_advisory_lock(%s, %s)", (_LOCK_NAMESPACE, _LOCK_ID))
    try:
        connection.execute(f"CREATE SCHEMA IF NOT EXISTS {quoted_schema}")
        connection.execute(f"SET search_path TO {quoted_schema}")
        existing = connection.execute(
            """
            SELECT COUNT(*) AS relation_count
              FROM pg_catalog.pg_class c
              JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = %s
               AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')
            """,
            (schema,),
        ).fetchone()
        count = existing["relation_count"] if isinstance(existing, dict) else existing[0]
        if int(count) != 0:
            raise RuntimeError(
                f"Refusing to install PostgreSQL baseline: schema {schema!r} is not empty."
            )

        with connection.transaction():
            for statement in ddl:
                connection.execute(statement)
            connection.execute(
                """
                CREATE TABLE "nexus_schema_installation" (
                    id SMALLINT PRIMARY KEY CHECK (id = 1),
                    baseline_sha256 CHAR(64) NOT NULL,
                    installed_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            connection.execute(
                'INSERT INTO "nexus_schema_installation" (id, baseline_sha256) VALUES (1, %s)',
                (digest,),
            )
        return digest
    finally:
        connection.execute("SELECT pg_advisory_unlock(%s, %s)", (_LOCK_NAMESPACE, _LOCK_ID))


def install_postgres_baseline_file(connection, baseline_path: str | Path, *, schema: str = "nexus") -> str:
    """Read a generated UTF-8 baseline file and install it into a fresh schema."""
    path = Path(baseline_path)
    if not path.is_file():
        raise FileNotFoundError(f"PostgreSQL baseline file not found: {path}")
    return install_postgres_baseline(
        connection, path.read_text(encoding="utf-8"), schema=schema
    )
