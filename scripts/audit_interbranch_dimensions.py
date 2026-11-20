"""Read-only audit of historical inter-branch stock transfer dimensions.

The audit never updates data. It compares posted transfer journal lines with
the *current* warehouse-to-branch mapping and explicitly flags that current
mapping cannot prove historical ownership if warehouse assignments changed.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


def _columns(conn, table):
    return {row["name"] for row in conn.execute(f'PRAGMA table_info("{table}")')}


def audit_interbranch_dimensions(database_path):
    path = Path(database_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Database file does not exist: {path}")

    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        tables = {row["name"] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        required = {"stock_movements", "stock_warehouses", "journal_entries", "journal_lines"}
        missing = sorted(required - tables)
        if missing:
            raise ValueError("Required audit tables are missing: " + ", ".join(missing))

        movement_cols = _columns(conn, "stock_movements")
        warehouse_cols = _columns(conn, "stock_warehouses")
        line_cols = _columns(conn, "journal_lines")
        entry_cols = _columns(conn, "journal_entries")
        needed = {
            "stock_movements": {"id", "movement_no", "movement_type", "movement_date",
                                "source_warehouse_id", "target_warehouse_id", "journal_entry_id",
                                "accounting_status"},
            "stock_warehouses": {"id", "code", "name_ar", "branch_id"},
            "journal_entries": {"id", "status"},
            "journal_lines": {"entry_id", "account_id", "debit", "credit", "branch_id",
                              "stock_product_id", "stock_warehouse_id"},
        }
        actual = {
            "stock_movements": movement_cols,
            "stock_warehouses": warehouse_cols,
            "journal_entries": entry_cols,
            "journal_lines": line_cols,
        }
        schema_gaps = {
            table: sorted(columns - actual[table])
            for table, columns in needed.items()
            if columns - actual[table]
        }
        if schema_gaps:
            raise ValueError("Required audit columns are missing: " +
                             json.dumps(schema_gaps, ensure_ascii=False))

        rows = conn.execute("""
            SELECT m.id, m.movement_no, m.movement_date, m.journal_entry_id,
                   m.accounting_status, m.source_warehouse_id, m.target_warehouse_id,
                   sw.code AS source_code, sw.name_ar AS source_name,
                   sw.branch_id AS source_branch_id,
                   tw.code AS target_code, tw.name_ar AS target_name,
                   tw.branch_id AS target_branch_id,
                   je.status AS journal_status
              FROM stock_movements m
              LEFT JOIN stock_warehouses sw ON sw.id=m.source_warehouse_id
              LEFT JOIN stock_warehouses tw ON tw.id=m.target_warehouse_id
              LEFT JOIN journal_entries je ON je.id=m.journal_entry_id
             WHERE lower(m.movement_type)='transfer'
             ORDER BY m.movement_date, m.id
        """).fetchall()

        results = []
        for movement in rows:
            issues = []
            entry_id = movement["journal_entry_id"]
            if not entry_id:
                issues.append("missing_journal_link")
                lines = []
            else:
                lines = conn.execute("""
                    SELECT jl.branch_id, jl.stock_product_id, jl.stock_warehouse_id,
                           jl.account_id, a.code AS account_code
                      FROM journal_lines jl
                      LEFT JOIN accounts a ON a.id=jl.account_id
                     WHERE jl.entry_id=?
                """, (entry_id,)).fetchall() if "accounts" in tables else conn.execute("""
                    SELECT branch_id, stock_product_id, stock_warehouse_id,
                           account_id, NULL AS account_code
                      FROM journal_lines WHERE entry_id=?
                """, (entry_id,)).fetchall()

            if entry_id and movement["journal_status"] != "posted":
                issues.append("journal_not_posted_or_missing")
            if entry_id and movement["accounting_status"] != "posted":
                issues.append("movement_status_not_posted")

            expected_by_warehouse = {
                movement["source_warehouse_id"]: movement["source_branch_id"],
                movement["target_warehouse_id"]: movement["target_branch_id"],
            }
            inventory_lines = [
                line for line in lines
                if line["stock_warehouse_id"] in expected_by_warehouse
            ]
            if entry_id and not inventory_lines:
                issues.append("journal_has_no_stock_warehouse_dimensions")

            for line in inventory_lines:
                expected_branch = expected_by_warehouse[line["stock_warehouse_id"]]
                if line["stock_product_id"] is None:
                    issues.append("inventory_line_missing_product_dimension")
                if line["branch_id"] is None:
                    issues.append("inventory_line_missing_branch_dimension")
                elif expected_branch is not None and int(line["branch_id"]) != int(expected_branch):
                    issues.append("inventory_line_branch_differs_from_current_warehouse_mapping")
                elif expected_branch is None and line["branch_id"] is not None:
                    issues.append("warehouse_has_no_current_branch_but_journal_line_is_tagged")

            if movement["source_branch_id"] is None or movement["target_branch_id"] is None:
                issues.append("warehouse_missing_current_branch_mapping")
            # The inter-branch clearing account is activity-configurable. This
            # standalone audit deliberately does not guess its code; validation
            # belongs in the configured account-role audit.

            # Deduplicate issue codes while preserving order.
            issues = list(dict.fromkeys(issues))
            results.append({
                "movement_id": movement["id"],
                "movement_no": movement["movement_no"],
                "movement_date": movement["movement_date"],
                "journal_entry_id": entry_id,
                "accounting_status": movement["accounting_status"],
                "journal_status": movement["journal_status"],
                "source_warehouse": movement["source_code"],
                "source_branch_id_current": movement["source_branch_id"],
                "target_warehouse": movement["target_code"],
                "target_branch_id_current": movement["target_branch_id"],
                "journal_line_count": len(lines),
                "issues": issues,
                "requires_manual_review": bool(issues),
            })

        flagged = sum(1 for row in results if row["requires_manual_review"])
        return {
            "read_only": True,
            "movement_count": len(results),
            "flagged_count": flagged,
            "passed": flagged == 0,
            "historical_mapping_warning": (
                "Branch IDs are read from current warehouse mappings. This audit cannot "
                "prove which branch owned a warehouse on the original movement date; "
                "do not backfill historical branch dimensions from current mappings alone."
            ),
            "movements": results,
        }
    finally:
        conn.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite", required=True,
                        help="Path to an existing SQLite database; opened read-only.")
    args = parser.parse_args(argv)
    result = audit_interbranch_dimensions(args.sqlite)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
