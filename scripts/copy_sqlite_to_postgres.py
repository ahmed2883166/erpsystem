"""Transactional SQLite-to-PostgreSQL data copy for a fully matching, empty schema.

This is deliberately fail-closed: it refuses missing/extra columns, non-empty
target tables, broken SQLite integrity/foreign keys, and cyclic FK dependencies.
It never changes the SQLite source. Run only against an isolated PostgreSQL
schema after its complete NEXUS schema has been independently reviewed.
"""
from __future__ import annotations

import argparse
import heapq
import json
import os
import re
import sqlite3
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote


def normalize_value(value):
    """Avoid carrying SQLite binary floats into exact PostgreSQL NUMERIC columns."""
    if isinstance(value, float):
        return Decimal(str(value))
    return value


def quote_ident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def dependency_order(tables: list[str], foreign_keys: dict[str, set[str]]) -> list[str]:
    """Return parent-before-child order; reject cycles that need deferred FKs."""
    known = set(tables)
    dependencies = {
        table: set(foreign_keys.get(table, set())) & known
        for table in tables
    }
    dependents = {table: [] for table in tables}
    for child, parents in dependencies.items():
        for parent in parents:
            dependents[parent].append(child)
    for children in dependents.values():
        children.sort()

    ordered = []
    ready = [table for table, deps in dependencies.items() if not deps]
    heapq.heapify(ready)
    while ready:
        table = heapq.heappop(ready)
        ordered.append(table)
        for child in dependents[table]:
            dependencies[child].discard(table)
            if not dependencies[child]:
                heapq.heappush(ready, child)
    if len(ordered) != len(tables):
        cyclic = sorted(table for table, deps in dependencies.items() if deps)
        raise ValueError(
            "Foreign-key cycle detected; reviewed deferrable-constraint migration "
            "is required before copying: " + ", ".join(cyclic)
        )
    return ordered


def order_self_referencing_rows(rows, foreign_key_rows, table: str):
    """Parent-before-child ordering for self-FKs (e.g. account parent_id)."""
    groups = {}
    for fk in foreign_key_rows:
        groups.setdefault(fk["id"], []).append(fk)
    key_groups = []
    for group in groups.values():
        group.sort(key=lambda item: item["seq"])
        key_groups.append((
            [item["from"] for item in group],
            [item["to"] for item in group],
        ))
    if not key_groups or not rows:
        return rows
    if len(rows) > 250000:
        raise ValueError(
            f"Self-referencing table {table} exceeds the safe in-memory ordering limit."
        )

    parents_by_key = {}
    for index, row in enumerate(rows):
        for _, target_columns in key_groups:
            key = tuple(row[column] for column in target_columns)
            if any(value is None for value in key):
                continue
            parents_by_key.setdefault(tuple(target_columns), {})[key] = index

    dependencies = [set() for _ in rows]
    children = [[] for _ in rows]
    for child_index, row in enumerate(rows):
        for source_columns, target_columns in key_groups:
            child_key = tuple(row[column] for column in source_columns)
            if any(value is None for value in child_key):
                continue
            parent_index = parents_by_key[tuple(target_columns)].get(child_key)
            if parent_index is None:
                raise ValueError(
                    f"Self-reference in {table} points to a missing row; copy blocked."
                )
            if parent_index == child_index:
                raise ValueError(f"Self-reference cycle detected in {table}.")
            dependencies[child_index].add(parent_index)

    for child_index, parents in enumerate(dependencies):
        for parent_index in parents:
            children[parent_index].append(child_index)

    ready = [index for index, parents in enumerate(dependencies) if not parents]
    heapq.heapify(ready)
    ordered = []
    while ready:
        parent_index = heapq.heappop(ready)
        ordered.append(rows[parent_index])
        for child_index in children[parent_index]:
            dependencies[child_index].discard(parent_index)
            if not dependencies[child_index]:
                heapq.heappush(ready, child_index)
    if len(ordered) != len(rows):
        raise ValueError(f"Self-reference cycle detected in table {table}; copy blocked.")
    return ordered


def suspend_secondary_indexes(connection, schema: str) -> list[str]:
    """Drop rebuildable, non-constraint indexes inside the current transaction.

    Constraint-backed indexes and replica-identity indexes are retained.
    The caller must restore returned CREATE INDEX statements before commit.
    """
    rows = connection.execute(
        """
        SELECT index_class.relname AS index_name,
               pg_get_indexdef(index_info.indexrelid) AS index_definition
          FROM pg_index index_info
          JOIN pg_class table_class ON table_class.oid = index_info.indrelid
          JOIN pg_namespace table_ns ON table_ns.oid = table_class.relnamespace
          JOIN pg_class index_class ON index_class.oid = index_info.indexrelid
          LEFT JOIN pg_constraint constraint_info
            ON constraint_info.conindid = index_info.indexrelid
         WHERE table_ns.nspname = %s
           AND constraint_info.oid IS NULL
           AND NOT index_info.indisreplident
         ORDER BY table_class.relname, index_class.relname
        """,
        (schema,),
    ).fetchall()
    definitions = []
    for row in rows:
        index_name = row["index_name"]
        definition = row["index_definition"]
        connection.execute(
            "DROP INDEX " + quote_ident(schema) + "." + quote_ident(index_name)
        )
        definitions.append(definition)
    return definitions


def restore_secondary_indexes(connection, definitions: list[str]) -> int:
    """Recreate indexes captured by suspend_secondary_indexes before commit."""
    for definition in definitions:
        if not isinstance(definition, str) or not definition.lstrip().upper().startswith("CREATE "):
            raise ValueError("Refusing to restore an invalid PostgreSQL index definition.")
        connection.execute(definition)
    return len(definitions)


def suspend_dashboard_version_triggers(connection, schema: str) -> list[dict]:
    """Temporarily remove only dashboard cache-version triggers in this schema.

    PostgreSQL DDL is transactional: a failed copy rolls the DROP TRIGGER
    statements back, leaving the target's original triggers in place.
    Trigger functions and audit-protection triggers are never modified.
    """
    rows = connection.execute(
        """
        SELECT table_class.relname AS table_name,
               trigger_info.tgname AS trigger_name,
               trigger_info.tgenabled AS enabled_state,
               pg_get_triggerdef(trigger_info.oid, true) AS trigger_definition
          FROM pg_trigger trigger_info
          JOIN pg_class table_class ON table_class.oid = trigger_info.tgrelid
          JOIN pg_namespace table_ns ON table_ns.oid = table_class.relnamespace
         WHERE table_ns.nspname = %s
           AND NOT trigger_info.tgisinternal
           AND left(trigger_info.tgname, length('trg_dashboard_version_')) =
               'trg_dashboard_version_'
         ORDER BY table_class.relname, trigger_info.tgname
        """,
        (schema,),
    ).fetchall()
    suspended = []
    for row in rows:
        item = {
            "table_name": row["table_name"],
            "trigger_name": row["trigger_name"],
            "enabled_state": row["enabled_state"],
            "definition": row["trigger_definition"],
        }
        if not isinstance(item["definition"], str) or not item["definition"].lstrip().upper().startswith("CREATE "):
            raise ValueError("Refusing to suspend a dashboard trigger with an invalid definition.")
        connection.execute(
            "DROP TRIGGER " + quote_ident(item["trigger_name"]) +
            " ON " + quote_ident(schema) + "." + quote_ident(item["table_name"])
        )
        suspended.append(item)
    return suspended


def restore_dashboard_version_triggers(connection, schema: str, triggers: list[dict]) -> int:
    """Restore dashboard triggers and preserve their original enabled states."""
    for item in triggers:
        connection.execute(item["definition"])
        table_name = quote_ident(schema) + "." + quote_ident(item["table_name"])
        trigger_name = quote_ident(item["trigger_name"])
        enabled_state = item["enabled_state"]
        if enabled_state == "D":
            command = "DISABLE TRIGGER"
        elif enabled_state == "R":
            command = "ENABLE REPLICA TRIGGER"
        elif enabled_state == "A":
            command = "ENABLE ALWAYS TRIGGER"
        else:
            command = "ENABLE TRIGGER"
        connection.execute(
            "ALTER TABLE " + table_name + " " + command + " " + trigger_name
        )
    return len(triggers)


def bump_dashboard_version_once(connection, schema: str) -> int:
    """Invalidate dashboard snapshots once after all copied rows are loaded."""
    row = connection.execute(
        "UPDATE " + quote_ident(schema) + "." + quote_ident("management_dashboard_data_version") +
        " SET version = version + 1 WHERE id = 1 RETURNING version"
    ).fetchone()
    if row is None:
        raise RuntimeError(
            "Dashboard version row id=1 is missing; refusing to accept the copy."
        )
    return row["version"]


def copy_rows_with_postgres_copy(connection, copy_sql: str, rows, columns: list[str]) -> int:
    """Stream adapted row tuples through psycopg 3 COPY FROM STDIN."""
    copied = 0
    with connection.cursor() as cursor:
        with cursor.copy(copy_sql) as copy:
            for row in rows:
                copy.write_row(
                    tuple(normalize_value(row[column]) for column in columns)
                )
                copied += 1
    return copied


def open_sqlite_readonly(path: str):
    source = Path(path).expanduser().resolve(strict=True)
    if not source.is_file():
        raise ValueError("SQLite source must be an existing database file.")
    uri = "file:" + quote(source.as_posix(), safe="/:") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def copy_database(sqlite_path: str, postgres_dsn: str, schema: str = "nexus",
                  batch_size: int = 500, *, apply: bool = False) -> dict:
    if not apply:
        raise ValueError("Copy is disabled by default. Pass apply=True only after reviewing the isolated target.")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", schema):
        raise ValueError("PostgreSQL schema must be a simple identifier.")
    if schema.lower() == "public":
        raise ValueError("Copy requires a dedicated, isolated PostgreSQL schema; public is blocked.")
    if batch_size < 1 or batch_size > 10000:
        raise ValueError("batch_size must be between 1 and 10000.")
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise RuntimeError("Install psycopg[binary]>=3.2,<4.0 first.") from exc

    src = open_sqlite_readonly(sqlite_path)
    dst = None
    try:
        # Pin one read snapshot for the entire copy so concurrent application
        # writes cannot produce a cross-table mixture of different source moments.
        src.execute("BEGIN")
        dst = psycopg.connect(postgres_dsn, connect_timeout=10, autocommit=False,
                              row_factory=dict_row)
        integrity = [r[0] for r in src.execute("PRAGMA integrity_check")]
        if integrity != ["ok"]:
            raise ValueError("SQLite integrity_check failed; source copy is blocked.")
        fk_issues = [dict(r) for r in src.execute("PRAGMA foreign_key_check")]
        if fk_issues:
            raise ValueError(f"SQLite foreign_key_check failed: {fk_issues[:20]}")

        tables = [r["name"] for r in src.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )]
        if not tables:
            raise ValueError("SQLite source contains no application tables.")

        source_columns = {}
        foreign_keys = {}
        self_foreign_keys = {}
        for table in tables:
            qtable = quote_ident(table)
            source_columns[table] = [
                r["name"] for r in src.execute(f"PRAGMA table_info({qtable})")
            ]
            fk_rows = [dict(r) for r in src.execute(f"PRAGMA foreign_key_list({qtable})")]
            foreign_keys[table] = {r["table"] for r in fk_rows if r["table"] != table}
            self_foreign_keys[table] = [r for r in fk_rows if r["table"] == table]

        qschema = quote_ident(schema)
        try:
            # PostgreSQL transaction characteristics must be set before
            # any other statement starts the implicit transaction.
            dst.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
            # Keep the migration transaction durable: do not acknowledge the
            # copy commit before PostgreSQL has flushed it to durable storage.
            # This applies to the isolated target schema as well as rehearsals.
            dst.execute("SET LOCAL synchronous_commit TO ON")
            dst.execute("SET LOCAL maintenance_work_mem TO '128MB'")
            dst.execute("SET LOCAL search_path TO " + qschema)
            target_tables = {
                r["table_name"] for r in dst.execute(
                    """SELECT table_name FROM information_schema.tables
                       WHERE table_schema = %s AND table_type = 'BASE TABLE'""",
                    (schema,),
                ).fetchall()
                if r["table_name"] != "nexus_schema_installation"
            }
            missing = sorted(set(tables) - target_tables)
            extra = sorted(target_tables - set(tables))
            if missing:
                raise ValueError("Target schema is missing tables: " + ", ".join(missing))
            if extra:
                raise ValueError("Target schema contains tables outside the source schema: " + ", ".join(extra))

            for table in tables:
                target_cols = [
                    r["column_name"] for r in dst.execute(
                        """SELECT column_name FROM information_schema.columns
                           WHERE table_schema = %s AND table_name = %s
                           ORDER BY ordinal_position""",
                        (schema, table),
                    ).fetchall()
                ]
                if set(target_cols) != set(source_columns[table]):
                    raise ValueError(
                        f"Column mismatch for {table}: SQLite={source_columns[table]}, "
                        f"PostgreSQL={target_cols}"
                    )
                qname = qschema + "." + quote_ident(table)
                count = dst.execute(f"SELECT COUNT(*) AS n FROM {qname}").fetchone()["n"]
                if count:
                    raise ValueError(
                        f"Target table {table} is not empty ({count} rows); copy blocked."
                    )

            order = dependency_order(tables, foreign_keys)

            # Keep FK/unique-constraint and replica-identity indexes intact.
            # Rebuildable secondary indexes and dashboard cache-version
            # triggers are suspended only inside this isolated transaction.
            # Audit-protection triggers remain enabled throughout.
            suspended_indexes = suspend_secondary_indexes(dst, schema)
            suspended_dashboard_triggers = suspend_dashboard_version_triggers(dst, schema)
            counts = {}
            for table in order:
                columns = source_columns[table]
                qcols = ", ".join(quote_ident(c) for c in columns)
                qname = qschema + "." + quote_ident(table)
                copy_sql = f"COPY {qname} ({qcols}) FROM STDIN"
                cursor = src.execute(f"SELECT {qcols} FROM {quote_ident(table)}")
                self_fk_rows = self_foreign_keys[table]
                copied = 0
                if self_fk_rows:
                    ordered_rows = order_self_referencing_rows(
                        cursor.fetchall(), self_fk_rows, table
                    )
                    row_batches = (
                        ordered_rows[index:index + batch_size]
                        for index in range(0, len(ordered_rows), batch_size)
                    )
                else:
                    def fetch_batches():
                        while True:
                            batch = cursor.fetchmany(batch_size)
                            if not batch:
                                break
                            yield batch
                    row_batches = fetch_batches()
                for rows in row_batches:
                    if not rows:
                        break
                    copied += copy_rows_with_postgres_copy(
                        dst, copy_sql, rows, columns
                    )
                target_count = dst.execute(
                    f"SELECT COUNT(*) AS n FROM {qname}"
                ).fetchone()["n"]
                if target_count != copied:
                    raise RuntimeError(
                        f"Row count mismatch for {table}: copied={copied}, target={target_count}"
                    )
                counts[table] = copied

            # Rebuild every secondary index before the copy can be accepted.
            # Any duplicate/data incompatibility fails the transaction and
            # restores the original target indexes on rollback.
            restored_indexes = restore_secondary_indexes(dst, suspended_indexes)
            restored_dashboard_triggers = restore_dashboard_version_triggers(
                dst, schema, suspended_dashboard_triggers
            )
            # All row-level dashboard invalidations are coalesced into one
            # version bump after the triggers have been restored.
            dashboard_version = bump_dashboard_version_once(dst, schema)

            # Force all deferred PostgreSQL constraints to be checked before sequence updates/commit.
            # Immediate constraints have already been checked by PostgreSQL during inserts.
            dst.execute("SET CONSTRAINTS ALL IMMEDIATE")

            # Explicit IDs are preserved; reset identity/serial sequences to the new maximum.
            for table in order:
                for col in source_columns[table]:
                    seq = dst.execute(
                        "SELECT pg_get_serial_sequence(%s, %s) AS sequence_name",
                        (f"{quote_ident(schema)}.{quote_ident(table)}", col),
                    ).fetchone()["sequence_name"]
                    if seq:
                        qname = qschema + "." + quote_ident(table)
                        qcol = quote_ident(col)
                        dst.execute(
                            """SELECT setval(%s, GREATEST(COALESCE(MAX(""" + qcol +
                            """), 1), 1), MAX(""" + qcol + """) IS NOT NULL)
                               FROM """ + qname,
                            (seq,),
                        )
            dst.commit()
            return {
                "committed": True,
                "source_read_only": True,
                "schema": schema,
                "table_order": order,
                "table_row_counts": counts,
                "total_rows": sum(counts.values()),
                "secondary_indexes_rebuilt": restored_indexes,
                "dashboard_triggers_rebuilt": restored_dashboard_triggers,
                "dashboard_version_after_copy": dashboard_version,
            }
        except Exception:
            dst.rollback()
            raise
    finally:
        src.close()
        if dst is not None:
            dst.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite", required=True, help="Existing SQLite database file")
    parser.add_argument("--postgres-dsn", default=os.environ.get("NEXUS_POSTGRES_DSN"))
    parser.add_argument("--schema", default="nexus")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--apply", action="store_true", help="Explicitly authorize writes to the isolated empty target schema")
    args = parser.parse_args(argv)
    if not args.postgres_dsn:
        parser.error("Provide --postgres-dsn or set NEXUS_POSTGRES_DSN.")
    try:
        result = copy_database(args.sqlite, args.postgres_dsn, args.schema, args.batch_size, apply=args.apply)
    except Exception as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
