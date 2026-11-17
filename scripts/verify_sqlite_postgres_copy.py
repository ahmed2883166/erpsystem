"""Read-only post-copy verification for SQLite -> PostgreSQL NEXUS migrations.

Checks every table/column/row count plus exact-decimal accounting, party, and
inventory valuation control totals. It never changes either database.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import quote



def _ident(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", value):
        raise ValueError(f"Unsafe PostgreSQL identifier: {value!r}")
    return '"' + value.replace('"', '""') + '"'


def _decimal(value) -> Decimal:
    if value is None or (isinstance(value, str) and not value.strip()):
        return Decimal("0")
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Non-numeric value found in financial control field: {value!r}") from exc


def _sqlite_grouped_sum(conn, table, amount_column, group_columns=()):
    qtable = '"' + table.replace('"', '""') + '"'
    qamount = '"' + amount_column.replace('"', '""') + '"'
    qgroups = [ '"' + col.replace('"', '""') + '"' for col in group_columns ]
    select = ", ".join(qgroups + [qamount])
    rows = conn.execute(f"SELECT {select} FROM {qtable}").fetchall()
    totals = {}
    for row in rows:
        key = tuple(row[i] for i in range(len(group_columns)))
        totals[key] = totals.get(key, Decimal("0")) + _decimal(row[len(group_columns)])
    return totals


def _pg_grouped_sum(conn, schema, table, amount_column, group_columns=()):
    qtable = _ident(schema) + "." + _ident(table)
    qamount = _ident(amount_column)
    qgroups = [_ident(col) for col in group_columns]
    group_sql = ", ".join(qgroups)
    select_sql = (group_sql + ", " if group_sql else "") + (
        "COALESCE(SUM(COALESCE(NULLIF(BTRIM(" + qamount +
        "::text), ''), '0')::numeric), 0) AS total_value"
    )
    sql = f"SELECT {select_sql} FROM {qtable}"
    if group_sql:
        sql += f" GROUP BY {group_sql}"
    result = {}
    for row in conn.execute(sql).fetchall():
        key = tuple(row[col] for col in group_columns)
        result[key] = _decimal(row["total_value"])
    return result


def _compare_decimal_maps(label, source, target, issues):
    keys = sorted(set(source) | set(target), key=lambda item: tuple(str(x) for x in item))
    for key in keys:
        left = source.get(key, Decimal("0"))
        right = target.get(key, Decimal("0"))
        if left != right:
            issues.append({
                "control": label,
                "key": list(key),
                "sqlite": str(left),
                "postgres": str(right),
            })


def _missing_control_columns(required, source_available, target_available):
    """Return explicit source/target schema gaps for a requested control."""
    required = set(required)
    source_missing = sorted(required - set(source_available))
    target_missing = sorted(required - set(target_available))
    return source_missing, target_missing


def _journal_entry_key(columns):
    """Identify the journal-line FK needed for per-entry control totals."""
    available = set(columns)
    return next(
        (name for name in ("journal_entry_id", "entry_id", "journal_id")
         if name in available),
        None,
    )


def _inventory_valuation_required(source_tables):
    """Require valuation controls whenever core inventory tables are present."""
    inventory_core = {"stock_products", "stock_movements", "stock_warehouses"}
    return bool(set(source_tables) & inventory_core)


def _sqlite_foreign_key_map(conn, tables):
    """Return normalized FK definitions, preserving composite-key column order."""
    result = []
    for table in sorted(tables):
        quoted = '"' + table.replace('"', '""') + '"'
        rows = conn.execute(f"PRAGMA foreign_key_list({quoted})").fetchall()
        grouped = {}
        for row in rows:
            grouped.setdefault(row["id"], []).append(row)
        for key in sorted(grouped):
            parts = sorted(grouped[key], key=lambda item: item["seq"])
            result.append((
                table,
                tuple(item["from"] for item in parts),
                parts[0]["table"],
                tuple(item["to"] for item in parts),
                str(parts[0]["on_update"]).upper(),
                str(parts[0]["on_delete"]).upper(),
            ))
    return sorted(result)


def _pg_foreign_key_map(conn, schema):
    """Read PostgreSQL FK definitions in ordinal order, including composite keys."""
    rows = conn.execute(
        """
        SELECT src.relname AS table_name,
               src_att.attname AS column_name,
               tgt.relname AS foreign_table_name,
               tgt_att.attname AS foreign_column_name,
               con.confupdtype AS update_action,
               con.confdeltype AS delete_action,
               ord.ordinality,
               con.conname AS constraint_name
          FROM pg_constraint con
          JOIN pg_class src ON src.oid = con.conrelid
          JOIN pg_namespace src_ns ON src_ns.oid = src.relnamespace
          JOIN pg_class tgt ON tgt.oid = con.confrelid
          JOIN LATERAL unnest(con.conkey, con.confkey)
               WITH ORDINALITY AS ord(source_attnum, target_attnum, ordinality)
            ON TRUE
          JOIN pg_attribute src_att
            ON src_att.attrelid = src.oid AND src_att.attnum = ord.source_attnum
          JOIN pg_attribute tgt_att
            ON tgt_att.attrelid = tgt.oid AND tgt_att.attnum = ord.target_attnum
         WHERE con.contype = 'f' AND src_ns.nspname = %s
         ORDER BY src.relname, con.conname, ord.ordinality
        """,
        (schema,),
    ).fetchall()
    actions = {
        "a": "NO ACTION",
        "r": "RESTRICT",
        "c": "CASCADE",
        "n": "SET NULL",
        "d": "SET DEFAULT",
    }
    grouped = {}
    for row in rows:
        key = (row["table_name"], row["constraint_name"])
        grouped.setdefault(key, []).append(row)
    result = []
    for (table, _), parts in grouped.items():
        parts.sort(key=lambda item: item["ordinality"])
        result.append((
            table,
            tuple(item["column_name"] for item in parts),
            parts[0]["foreign_table_name"],
            tuple(item["foreign_column_name"] for item in parts),
            actions[parts[0]["update_action"]],
            actions[parts[0]["delete_action"]],
        ))
    return sorted(result)


def _foreign_key_differences(source, target):
    """Report missing and unexpected FK relationships without hiding duplicates."""
    from collections import Counter
    source_counts = Counter(source)
    target_counts = Counter(target)
    missing = []
    extra = []
    for item in source:
        if target_counts[item]:
            target_counts[item] -= 1
        else:
            missing.append(item)
    for item in target:
        if source_counts[item]:
            source_counts[item] -= 1
        else:
            extra.append(item)
    return missing, extra


def _verification_passed(issues, controls_skipped, row_counts, source_tables):
    """A copy cannot pass while any required financial control was skipped."""
    return (
        not issues
        and not controls_skipped
        and len(row_counts) == len(source_tables)
    )


def verify_copy(sqlite_path: str, postgres_dsn: str, schema: str = "nexus") -> dict:
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise RuntimeError("Install psycopg[binary]>=3.2,<4.0 to verify PostgreSQL copies.") from exc
    _ident(schema)
    source_path = Path(sqlite_path).expanduser().resolve(strict=True)
    uri = "file:" + quote(source_path.as_posix(), safe="/:") + "?mode=ro"
    src = sqlite3.connect(uri, uri=True, timeout=10)
    src.row_factory = sqlite3.Row
    dst = None
    try:
        src.execute("PRAGMA query_only = ON")
        src.execute("BEGIN")
        integrity = [row[0] for row in src.execute("PRAGMA integrity_check")]
        fk_issues = [dict(row) for row in src.execute("PRAGMA foreign_key_check")]
        if integrity != ["ok"] or fk_issues:
            raise ValueError("SQLite source integrity/FK checks failed; verification is blocked.")

        source_tables = [row[0] for row in src.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )]
        source_columns = {
            table: [row["name"] for row in src.execute(
                'PRAGMA table_info("' + table.replace('"', '""') + '")'
            )]
            for table in source_tables
        }
        dst = psycopg.connect(postgres_dsn, connect_timeout=10, autocommit=False,
                              row_factory=dict_row)
        dst.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        dst.execute("SET LOCAL search_path TO " + _ident(schema))
        target_tables = {
            row["table_name"] for row in dst.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema=%s AND table_type='BASE TABLE'", (schema,)
            ).fetchall()
        }
        issues = []
        missing = sorted(set(source_tables) - target_tables)
        extra = sorted(target_tables - set(source_tables))
        if missing or extra:
            issues.append({"control": "table_coverage", "missing": missing, "extra": extra})

        row_counts = {}
        target_columns_by_table = {}
        for table in source_tables:
            if table not in target_tables:
                continue
            src_cols = source_columns[table]
            target_cols = [row["column_name"] for row in dst.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position",
                (schema, table),
            ).fetchall()]
            target_columns_by_table[table] = target_cols
            if set(src_cols) != set(target_cols):
                issues.append({
                    "control": "column_coverage", "table": table,
                    "missing": sorted(set(src_cols) - set(target_cols)),
                    "extra": sorted(set(target_cols) - set(src_cols)),
                })
                continue
            source_count = src.execute(
                'SELECT COUNT(*) FROM "' + table.replace('"', '""') + '"'
            ).fetchone()[0]
            target_count = dst.execute(
                "SELECT COUNT(*) AS n FROM " + _ident(schema) + "." + _ident(table)
            ).fetchone()["n"]
            row_counts[table] = {"sqlite": source_count, "postgres": target_count}
            if source_count != target_count:
                issues.append({
                    "control": "row_count", "table": table,
                    "sqlite": source_count, "postgres": target_count,
                })

        controls = []
        controls_skipped = []
        sqlite_fks = _sqlite_foreign_key_map(src, source_tables)
        postgres_fks = _pg_foreign_key_map(dst, schema)
        missing_fks, extra_fks = _foreign_key_differences(sqlite_fks, postgres_fks)
        if missing_fks or extra_fks:
            issues.append({
                "control": "foreign_key_coverage",
                "missing_in_postgres": [list(item) for item in missing_fks[:200]],
                "extra_in_postgres": [list(item) for item in extra_fks[:200]],
                "source_count": len(sqlite_fks),
                "target_count": len(postgres_fks),
            })
        def compare_control(table, amount, groups, label):
            if table not in source_tables or table not in target_tables:
                controls_skipped.append({
                    "control": label,
                    "reason": "table_missing",
                })
                return
            required = {amount, *groups}
            source_available = set(source_columns.get(table, []))
            target_available = set(target_columns_by_table.get(table, []))
            missing_source, missing_target = _missing_control_columns(
                required, source_available, target_available
            )
            if missing_source or missing_target:
                controls_skipped.append({
                    "control": label,
                    "reason": "required_columns_missing",
                    "missing_in_sqlite": missing_source,
                    "missing_in_postgres": missing_target,
                })
                return
            source_map = _sqlite_grouped_sum(src, table, amount, groups)
            target_map = _pg_grouped_sum(dst, schema, table, amount, groups)
            _compare_decimal_maps(label, source_map, target_map, issues)
            controls.append(label)

        if "journal_lines" not in source_tables:
            controls_skipped.append({
                "control": "journal_lines.required",
                "reason": "source_table_missing",
            })
        if "journal_lines" in source_tables:
            compare_control("journal_lines", "debit", (), "journal_lines.debit_total")
            compare_control("journal_lines", "credit", (), "journal_lines.credit_total")
            # Compare totals per journal as well as globally. This catches a
            # migration that preserves grand totals but assigns lines to the
            # wrong entry. Support the common legacy FK column spellings.
            journal_columns = set(source_columns["journal_lines"])
            entry_key = _journal_entry_key(journal_columns)
            if entry_key:
                compare_control(
                    "journal_lines", "debit", (entry_key,),
                    "journal_lines.debit_by_entry",
                )
                compare_control(
                    "journal_lines", "credit", (entry_key,),
                    "journal_lines.credit_by_entry",
                )
            else:
                controls_skipped.append({
                    "control": "journal_lines.by_entry",
                    "reason": "journal_entry_reference_missing",
                    "expected_one_of": ["journal_entry_id", "entry_id", "journal_id"],
                })
            if "customer_id" in journal_columns:
                compare_control("journal_lines", "debit", ("customer_id",), "customer.debit_by_id")
                compare_control("journal_lines", "credit", ("customer_id",), "customer.credit_by_id")
            if "supplier_id" in journal_columns:
                compare_control("journal_lines", "debit", ("supplier_id",), "supplier.debit_by_id")
                compare_control("journal_lines", "credit", ("supplier_id",), "supplier.credit_by_id")

        if (
            _inventory_valuation_required(source_tables)
            and "stock_valuation_layers" not in source_tables
        ):
            controls_skipped.append({
                "control": "stock_valuation_layers.required",
                "reason": "source_table_missing",
            })
        if "stock_valuation_layers" in source_tables:
            for amount in ("qty_in", "qty_remaining", "value_remaining"):
                compare_control("stock_valuation_layers", amount, (), f"stock_valuation_layers.{amount}_total")
                compare_control(
                    "stock_valuation_layers", amount, ("product_id", "warehouse_id"),
                    f"stock_valuation_layers.{amount}_by_product_warehouse",
                )

        dst.execute("SET CONSTRAINTS ALL IMMEDIATE")
        dst.rollback()
        return {
            "read_only": True,
            "source_integrity_ok": True,
            "source_foreign_keys_ok": True,
            "schema": schema,
            "source_table_count": len(source_tables),
            "matched_table_count": len(row_counts),
            "source_row_count": sum(item["sqlite"] for item in row_counts.values()),
            "target_row_count": sum(item["postgres"] for item in row_counts.values()),
            "financial_and_inventory_controls": controls,
            "financial_and_inventory_controls_skipped": controls_skipped,
            "issue_count": len(issues),
            "issues": issues[:200],
            "passed": _verification_passed(
                issues, controls_skipped, row_counts, source_tables
            ),
            "requires_review": bool(controls_skipped),
            "row_counts": row_counts,
        }
    finally:
        src.close()
        if dst is not None:
            dst.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite", required=True)
    parser.add_argument("--postgres-dsn", default=os.environ.get("NEXUS_POSTGRES_DSN"))
    parser.add_argument("--schema", default="nexus")
    args = parser.parse_args(argv)
    if not args.postgres_dsn:
        parser.error("Provide --postgres-dsn or set NEXUS_POSTGRES_DSN.")
    result = verify_copy(args.sqlite, args.postgres_dsn, args.schema)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
