"""Read-only preflight audit for moving a NEXUS SQLite database to PostgreSQL.

This command never writes to either database. It checks exact table/column
coverage, target emptiness, and SQLite integrity before a separate copy run.
"""
from __future__ import annotations

import argparse
import hashlib
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
from scripts.audit_postgres_numeric_data import audit_numeric_candidates
from scripts.inspect_sqlite_schema import inspect_database
from scripts.generate_postgres_schema import pg_type


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
            column_rows = conn.execute(f"PRAGMA table_info({quoted})").fetchall()
            columns = [row["name"] for row in column_rows]
            column_types = {
                row["name"]: (row["type"] or "").strip().upper()
                for row in column_rows
            }
            count = conn.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0]
            tables[name] = {
                "columns": columns, "column_types": column_types, "row_count": count
            }
        foreign_keys = _sqlite_foreign_key_map(conn, list(tables))
        duplicate_supplier_invoice_numbers = []
        supplier_bill_columns = set(tables.get("supplier_bills", {}).get("columns", []))
        if {"supplier_id", "supplier_invoice_no"}.issubset(supplier_bill_columns):
            duplicate_supplier_invoice_numbers = [
                {
                    "supplier_id": row["supplier_id"],
                    "normalized_invoice_no": row["normalized_invoice_no"],
                    "count": int(row["duplicate_count"]),
                }
                for row in conn.execute(
                    """SELECT supplier_id,
                              lower(trim(supplier_invoice_no)) AS normalized_invoice_no,
                              COUNT(*) AS duplicate_count
                       FROM supplier_bills
                       GROUP BY supplier_id, lower(trim(supplier_invoice_no))
                       HAVING COUNT(*) > 1
                       ORDER BY supplier_id, normalized_invoice_no"""
                ).fetchall()
            ]
        return {
            "path": str(source),
            "integrity_check": integrity,
            "foreign_key_violations": fk_issues,
            "foreign_keys": foreign_keys,
            "duplicate_supplier_invoice_numbers": duplicate_supplier_invoice_numbers,
            "stale_rebuild_tables": sorted(
                name for name in tables if name.endswith("_rebuilt")
            ),
            "tables": tables,
        }
    finally:
        conn.close()


def inspect_numeric_source(path: str) -> dict:
    """Run the exact-Decimal audit against the same read-only SQLite source."""
    snapshot = inspect_database(path, allow_non_temp=True)
    source = Path(path).expanduser().resolve(strict=True)
    uri = "file:" + quote(source.as_posix(), safe="/:") + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only = ON")
        return audit_numeric_candidates(connection, snapshot)
    finally:
        connection.close()


def inspect_postgres(dsn: str, schema: str) -> dict:
    if str(schema).lower() == "public":
        raise ValueError("PostgreSQL migration preflight refuses the shared public schema.")
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
            """SELECT table_name, column_name, data_type
               FROM information_schema.columns
               WHERE table_schema = %s
               ORDER BY table_name, ordinal_position""",
            (schema,),
        ).fetchall()
        tables: dict[str, dict] = {}
        for row in rows:
            tables.setdefault(
                row["table_name"],
                {"columns": [], "column_types": {}, "row_count": None},
            )
            tables[row["table_name"]]["columns"].append(row["column_name"])
            tables[row["table_name"]]["column_types"][row["column_name"]] = row["data_type"]
        foreign_keys = _pg_foreign_key_map(conn, schema)
        marker_present = "nexus_schema_installation" in tables
        baseline_sha256 = None
        if marker_present:
            marker_columns = set(tables["nexus_schema_installation"]["columns"])
            if {"id", "baseline_sha256"}.issubset(marker_columns):
                marker_row = conn.execute(
                    'SELECT baseline_sha256 FROM "' + schema +
                    '".nexus_schema_installation WHERE id = 1'
                ).fetchone()
                baseline_sha256 = marker_row["baseline_sha256"] if marker_row else None
        dashboard_version_present = "management_dashboard_data_version" in tables
        for name in tables:
            # Identifiers are quoted after validation against information_schema.
            quoted_schema = '"' + schema + '"'
            quoted_table = '"' + name.replace('"', '""') + '"'
            tables[name]["row_count"] = conn.execute(
                f"SELECT COUNT(*) FROM {quoted_schema}.{quoted_table}"
            ).fetchone()[0]
        conn.rollback()
        return {
            "schema": schema, "tables": tables, "foreign_keys": foreign_keys,
            "baseline_marker_present": marker_present,
            "baseline_sha256": baseline_sha256,
            "dashboard_version_table_present": dashboard_version_present,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def build_report(
    sqlite_info: dict, postgres_info: dict, numeric_audit: dict,
    expected_baseline_sha256: str | None = None,
) -> dict:
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
    type_issues = []
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
        source_types = details.get("column_types")
        target_types = target[table].get("column_types")
        if source_types is not None and target_types is not None:
            mismatches = []
            for column, source_type in source_types.items():
                if column not in target_types:
                    continue
                expected = pg_type(
                    {"name": column, "type": source_type}, table
                ).strip().lower()
                expected_family = (
                    "numeric" if expected.startswith("numeric")
                    else "double precision" if expected == "double precision"
                    else "bigint" if expected == "bigint"
                    else "bytea" if expected == "bytea"
                    else "text"
                )
                actual_family = str(target_types[column] or "").strip().lower()
                if actual_family != expected_family:
                    mismatches.append({
                        "column": column,
                        "sqlite_declared_type": source_type,
                        "expected_postgres_type": expected_family,
                        "actual_postgres_type": actual_family,
                    })
            if mismatches:
                type_issues.append({"table": table, "columns": mismatches})
        if target[table]["row_count"] != 0:
            count_differences.append({
                "table": table,
                "source_rows": details["row_count"],
                "target_rows": target[table]["row_count"],
                "reason": "target table is not empty; automatic copy is blocked",
            })
    source_ok = sqlite_info["integrity_check"] == ["ok"]
    fk_ok = not sqlite_info["foreign_key_violations"]
    numeric_ok = bool(numeric_audit.get("safe_for_numeric_migration"))
    marker_ok = postgres_info.get("baseline_marker_present", True)
    marker_hash = str(postgres_info.get("baseline_sha256") or "").strip()
    if expected_baseline_sha256 is not None:
        marker_ok = marker_ok and marker_hash == expected_baseline_sha256
    elif "baseline_marker_present" in postgres_info:
        marker_ok = marker_ok and bool(re.fullmatch(r"[0-9a-f]{64}", marker_hash))
    dashboard_version_ok = postgres_info.get("dashboard_version_table_present", True)
    duplicate_supplier_invoice_numbers = sqlite_info.get(
        "duplicate_supplier_invoice_numbers", []
    )
    stale_rebuild_tables = sqlite_info.get("stale_rebuild_tables", [])
    ready = (source_ok and fk_ok and numeric_ok and marker_ok and dashboard_version_ok
             and not missing_tables and not extra_tables
             and not column_issues and not count_differences
             and not missing_foreign_keys and not extra_foreign_keys
             and not duplicate_supplier_invoice_numbers and not stale_rebuild_tables
             and not type_issues)
    return {
        "read_only": True,
        "source_integrity_ok": source_ok,
        "source_foreign_keys_ok": fk_ok,
        "numeric_audit_passed": numeric_ok,
        "baseline_marker_valid": marker_ok,
        "baseline_sha256": marker_hash or None,
        "dashboard_version_table_present": dashboard_version_ok,
        "numeric_audit": numeric_audit,
        "missing_postgres_tables": missing_tables,
        "extra_postgres_tables": extra_tables,
        "column_differences": column_issues,
        "type_differences": type_issues,
        "foreign_key_differences": foreign_key_differences,
        "duplicate_supplier_invoice_numbers": duplicate_supplier_invoice_numbers,
        "stale_rebuild_tables": stale_rebuild_tables,
        "nonempty_postgres_tables": count_differences,
        "source_table_count": len(source),
        "source_row_count": sum(t["row_count"] for t in source.values()),
        "target_table_count": len(target),
        "preflight_passed": ready,
        "next_step": (
            "A separately reviewed data-copy run may be planned."
            if ready else
            "Do not copy yet: complete schema coverage, resolve source integrity and numeric "
            "audit issues, and use an empty isolated PostgreSQL target."
        ),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite", required=True, help="Path to the existing SQLite source database")
    parser.add_argument("--postgres-dsn", default=os.environ.get("NEXUS_POSTGRES_DSN"),
                        help="PostgreSQL DSN (or NEXUS_POSTGRES_DSN)")
    parser.add_argument("--schema", default="nexus", help="Target PostgreSQL schema")
    parser.add_argument("--baseline-sql", required=True,
                        help="Exact generated baseline SQL used to install the target schema")
    args = parser.parse_args(argv)
    if not args.postgres_dsn:
        parser.error("Provide --postgres-dsn or set NEXUS_POSTGRES_DSN.")
    try:
        sqlite_info = inspect_sqlite(args.sqlite)
        numeric_audit = inspect_numeric_source(args.sqlite)
        postgres_info = inspect_postgres(args.postgres_dsn, args.schema)
        expected_baseline_sha256 = hashlib.sha256(
            Path(args.baseline_sql).expanduser().read_text(encoding="utf-8").encode("utf-8")
        ).hexdigest()
        report = build_report(
            sqlite_info, postgres_info, numeric_audit, expected_baseline_sha256
        )
    except (OSError, sqlite3.Error, ValueError, RuntimeError) as exc:
        parser.error(str(exc))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["preflight_passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
