"""Read-only preflight audit for moving a NEXUS SQLite database to PostgreSQL.

This command never writes to either database. It checks exact table/column
coverage, target emptiness, and SQLite integrity before a separate copy run.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
from pathlib import Path
from urllib.parse import quote

from scripts.verify_sqlite_postgres_copy import (
    _foreign_key_differences,
    _pg_foreign_key_map,
    _sqlite_foreign_key_map,
)


def inspect_sqlite(path: str) -> dict:
    source = Path(path).expanduser().resolve(strict=True)
    if not source.is_file():
        raise ValueError("SQLite source must be an existing database file.")
    uri = "file:" + quote(source.as_posix(), safe="/:") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only = ON")
        integrity = [row[0] for row in conn.execute("PRAGMA integrity_check")]
        fk_issues = [dict(row) for row in conn.execute("PRAGMA foreign_key_check")]
        tables = {}
        names = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        for item in names:
            name = item["name"]
            quoted = '"' + name.replace('"', '""') + '"'
            columns = [row["name"] for row in conn.execute(
                f"PRAGMA table_info({quoted})"
            )]
            count = conn.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0]
            tables[name] = {"columns": columns, "row_count": count}
        foreign_keys = _sqlite_foreign_key_map(conn, list(tables))
        return {
            "path": str(source),
            "integrity_check": integrity,
            "foreign_key_violations": fk_issues,
            "foreign_keys": foreign_keys,
            "tables": tables,
        }
    finally:
        conn.close()


def inspect_postgres(dsn: str, schema: str) -> dict:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", schema):
        raise ValueError("PostgreSQL schema must be a simple identifier.")
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise RuntimeError("Install psycopg[binary]>=3.2,<4.0 first.") from exc

    conn = psycopg.connect(dsn, connect_timeout=10, autocommit=False, row_factory=dict_row)
    try:
        conn.execute("SET TRANSACTION READ ONLY")
        rows = conn.execute(
            """SELECT table_name, column_name
               FROM information_schema.columns
               WHERE table_schema = %s
               ORDER BY table_name, ordinal_position""",
            (schema,),
        ).fetchall()
        tables: dict[str, dict] = {}
        for row in rows:
            tables.setdefault(row["table_name"], {"columns": [], "row_count": None})
            tables[row["table_name"]]["columns"].append(row["column_name"])
        foreign_keys = _pg_foreign_key_map(conn, schema)
        for name in tables:
            # Identifiers are quoted after validation against information_schema.
            quoted_schema = '"' + schema + '"'
            quoted_table = '"' + name.replace('"', '""') + '"'
            tables[name]["row_count"] = conn.execute(
                f"SELECT COUNT(*) FROM {quoted_schema}.{quoted_table}"
            ).fetchone()[0]
        conn.rollback()
        return {"schema": schema, "tables": tables, "foreign_keys": foreign_keys}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def build_report(sqlite_info: dict, postgres_info: dict) -> dict:
    source = sqlite_info["tables"]
    # The PostgreSQL baseline installer owns this metadata table; it is not
    # application data and must not block a SQLite-to-PostgreSQL preflight.
    target = {
        name: details for name, details in postgres_info["tables"].items()
        if name != "nexus_schema_installation"
    }
    missing_tables = sorted(set(source) - set(target))
    extra_tables = sorted(set(target) - set(source))
    column_issues = []
    count_differences = []
    missing_foreign_keys, extra_foreign_keys = _foreign_key_differences(
        sqlite_info.get("foreign_keys", []), postgres_info.get("foreign_keys", [])
    )
    def foreign_key_json(item):
        return [
            list(value) if isinstance(value, tuple) else value
            for value in item
        ]

    foreign_key_differences = {
        "missing_in_postgres": [foreign_key_json(item) for item in missing_foreign_keys],
        "extra_in_postgres": [foreign_key_json(item) for item in extra_foreign_keys],
    }
    for table, details in source.items():
        if table not in target:
            continue
        src_cols = set(details["columns"])
        dst_cols = set(target[table]["columns"])
        missing = sorted(src_cols - dst_cols)
        extra = sorted(dst_cols - src_cols)
        if missing or extra:
            column_issues.append({
                "table": table,
                "missing_in_postgres": missing,
                "extra_in_postgres": extra,
            })
        if target[table]["row_count"] != 0:
            count_differences.append({
                "table": table,
                "source_rows": details["row_count"],
                "target_rows": target[table]["row_count"],
                "reason": "target table is not empty; automatic copy is blocked",
            })
    source_ok = sqlite_info["integrity_check"] == ["ok"]
    fk_ok = not sqlite_info["foreign_key_violations"]
    ready = (source_ok and fk_ok and not missing_tables and not extra_tables
             and not column_issues and not count_differences
             and not missing_foreign_keys and not extra_foreign_keys)
    return {
        "read_only": True,
        "source_integrity_ok": source_ok,
        "source_foreign_keys_ok": fk_ok,
        "missing_postgres_tables": missing_tables,
        "extra_postgres_tables": extra_tables,
        "column_differences": column_issues,
        "foreign_key_differences": foreign_key_differences,
        "nonempty_postgres_tables": count_differences,
        "source_table_count": len(source),
        "source_row_count": sum(t["row_count"] for t in source.values()),
        "target_table_count": len(target),
        "preflight_passed": ready,
        "next_step": (
            "A separately reviewed data-copy run may be planned."
            if ready else
            "Do not copy yet: complete schema coverage, resolve source integrity issues, "
            "and use an empty isolated PostgreSQL target."
        ),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite", required=True, help="Path to the existing SQLite source database")
    parser.add_argument("--postgres-dsn", default=os.environ.get("NEXUS_POSTGRES_DSN"),
                        help="PostgreSQL DSN (or NEXUS_POSTGRES_DSN)")
    parser.add_argument("--schema", default="nexus", help="Target PostgreSQL schema")
    args = parser.parse_args(argv)
    if not args.postgres_dsn:
        parser.error("Provide --postgres-dsn or set NEXUS_POSTGRES_DSN.")
    try:
        report = build_report(inspect_sqlite(args.sqlite), inspect_postgres(args.postgres_dsn, args.schema))
    except (OSError, sqlite3.Error, ValueError, RuntimeError) as exc:
        parser.error(str(exc))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["preflight_passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
