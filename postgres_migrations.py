"""Explicit, transactional PostgreSQL migration runner.

This runner is intentionally separate from Flask startup. It requires an
already-open PostgreSQL connection and a caller-supplied migration directory;
it never discovers a DSN, falls back to SQLite, or runs automatically.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from db_compat import execute_script, begin_write_transaction

_MIGRATION_FILE = re.compile(r"^(\d{4,})_([a-z0-9][a-z0-9_]*)\.sql$")
_SCHEMA_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ADVISORY_LOCK_NAMESPACE = 1313162835
_ADVISORY_LOCK_ID = 1


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str
    checksum: str


def quote_schema_name(schema: str) -> str:
    """Validate and quote a schema identifier (values cannot parameterize it)."""
    if not isinstance(schema, str) or not _SCHEMA_NAME.fullmatch(schema):
        raise ValueError("PostgreSQL schema must be a simple ASCII identifier.")
    if len(schema) > 63:
        raise ValueError("PostgreSQL schema identifiers cannot exceed 63 characters.")
    return '"' + schema + '"'


def load_migrations(directory: str | Path) -> list[Migration]:
    """Load ordered UTF-8 SQL migrations and calculate immutable checksums."""
    root = Path(directory)
    if not root.is_dir():
        raise FileNotFoundError(f"Migration directory does not exist: {root}")
    migrations = []
    seen_versions = set()
    for path in sorted(root.iterdir(), key=lambda item: item.name):
        if not path.is_file():
            continue
        match = _MIGRATION_FILE.fullmatch(path.name)
        if not match:
            raise ValueError(f"Invalid PostgreSQL migration filename: {path.name}")
        version = int(match.group(1))
        if version in seen_versions:
            raise ValueError(f"Duplicate PostgreSQL migration version: {version}")
        seen_versions.add(version)
        raw = path.read_bytes()
        try:
            sql = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"Migration must be UTF-8: {path.name}") from exc
        if not sql.strip():
            raise ValueError(f"Migration cannot be empty: {path.name}")
        migrations.append(Migration(
            version=version,
            name=path.name,
            sql=sql,
            checksum=hashlib.sha256(raw).hexdigest(),
        ))
    return migrations


def apply_migrations(connection, migrations, *, schema="nexus") -> list[int]:
    """Apply pending migrations atomically under a transaction-scoped PG lock.

    All pending DDL and version records commit together. A failed migration
    rolls the complete batch back. Applied migration files are immutable:
    changing an already-applied file is a hard error, not a silent re-run.
    """
    if getattr(connection, "autocommit", None) is not False:
        raise ValueError("Migration connection must use autocommit=False.")
    info = getattr(connection, "info", None)
    status = getattr(getattr(info, "transaction_status", None), "name", None)
    if status is not None and status != "IDLE":
        raise ValueError("Migration connection must be idle before execution.")

    quoted_schema = quote_schema_name(schema)
    ordered = sorted(migrations, key=lambda item: item.version)
    versions = [item.version for item in ordered]
    if len(versions) != len(set(versions)):
        raise ValueError("Duplicate PostgreSQL migration versions.")
    if any(not isinstance(item.version, int) or item.version < 1 for item in ordered):
        raise ValueError("Migration versions must be positive integers.")
    if any(not item.sql.strip() for item in ordered):
        raise ValueError("Migration SQL cannot be empty.")
    for item in ordered:
        match = _MIGRATION_FILE.fullmatch(str(item.name))
        if match is None or int(match.group(1)) != item.version:
            raise ValueError(
                f"Migration name {item.name!r} does not match version {item.version}."
            )
        expected_checksum = hashlib.sha256(item.sql.encode("utf-8")).hexdigest()
        if not re.fullmatch(r"[0-9a-f]{64}", str(item.checksum)):
            raise ValueError(f"Migration {item.name!r} has an invalid SHA-256 checksum.")
        if item.checksum != expected_checksum:
            raise ValueError(f"Migration {item.name!r} checksum does not match its SQL.")

    applied = []
    begin_write_transaction(connection)
    try:
        connection.execute(
            "SELECT pg_advisory_xact_lock(%s, %s)",
            (_ADVISORY_LOCK_NAMESPACE, _ADVISORY_LOCK_ID),
        )
        connection.execute(f"CREATE SCHEMA IF NOT EXISTS {quoted_schema}")
        # Unqualified migration DDL is confined to the explicitly selected schema.
        connection.execute(f"SET LOCAL search_path TO {quoted_schema}")
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {quoted_schema}."schema_migrations" (
                version BIGINT PRIMARY KEY,
                name TEXT NOT NULL,
                checksum CHAR(64) NOT NULL,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        rows = connection.execute(
            f'SELECT version, name, checksum FROM {quoted_schema}."schema_migrations"'
        ).fetchall()
        existing = {int(row["version"]): row for row in rows}
        available_versions = set(versions)
        unknown_versions = sorted(set(existing) - available_versions)
        if unknown_versions:
            rendered = ", ".join(str(version) for version in unknown_versions)
            raise RuntimeError(
                "Applied PostgreSQL migrations are missing from the supplied "
                f"migration set: {rendered}."
            )

        for migration in ordered:
            previous = existing.get(migration.version)
            if previous is not None:
                if previous["name"] != migration.name or previous["checksum"].strip() != migration.checksum:
                    raise RuntimeError(
                        f"Applied PostgreSQL migration {migration.version} was modified."
                    )
                continue

            execute_script(connection, migration.sql)
            connection.execute(
                f"""
                INSERT INTO {quoted_schema}."schema_migrations"
                    (version, name, checksum)
                VALUES (%s, %s, %s)
                """,
                (migration.version, migration.name, migration.checksum),
            )
            applied.append(migration.version)

        connection.commit()
        return applied
    except Exception:
        connection.rollback()
        raise
