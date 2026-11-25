"""Build a clean, fully initialized SQLite schema snapshot in an OS temp directory.

No existing NEXUS database is opened. All application paths are redirected to a
fresh temporary directory before importing app.py.
"""
import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path


def build_initialized_schema(database_output=None, seed_representative_data=False):
    repository_root = Path(__file__).resolve().parents[1]
    if str(repository_root) not in sys.path:
        sys.path.insert(0, str(repository_root))

    with tempfile.TemporaryDirectory(prefix="nexus-postgresql-schema-") as temp_root:
        temp_path = Path(temp_root)
        data_dir = temp_path / "data"
        backup_dir = temp_path / "backups"

        # These values are set before importing app_paths or app.py.
        os.environ["NEXUS_DATA_DIR"] = str(data_dir)
        os.environ["NEXUS_BACKUP_DIR"] = str(backup_dir)
        os.environ["NEXUS_DEMO_MODE"] = "1"
        os.environ["NEXUS_SKIP_DB_INIT"] = "1"
        os.environ["NEXUS_SECRET_KEY"] = "schema-inventory-test-key-" + ("x" * 40)

        import app as nexus_app
        from scripts.inspect_sqlite_schema import inspect_database

        # initialize_database() runs the real application schema initializers,
        # but only against the newly-created temporary database.
        nexus_app.initialize_database()
        if seed_representative_data:
            # Deterministic, non-sensitive accounting fixture. This is inserted
            # only into the disposable DB created above, never a user database.
            connection = nexus_app.db()
            try:
                accounts = connection.execute(
                    "SELECT id FROM accounts ORDER BY id LIMIT 2"
                ).fetchall()
                admin = connection.execute(
                    "SELECT id FROM users WHERE username='admin'"
                ).fetchone()
                if len(accounts) < 2 or not admin:
                    raise RuntimeError(
                        "Initialized NEXUS schema lacks seeded accounts or admin user."
                    )
                entry = connection.execute(
                    """INSERT INTO journal_entries
                       (entry_date,reference,description,created_by,created_at)
                       VALUES(?,?,?,?,?)""",
                    (
                        "2026-01-15",
                        "MIGRATION-REHEARSAL-001",
                        "Disposable migration rehearsal opening fixture",
                        admin["id"],
                        nexus_app.now(),
                    ),
                )
                connection.executemany(
                    """INSERT INTO journal_lines
                       (entry_id,account_id,debit,credit,memo)
                       VALUES(?,?,?,?,?)""",
                    [
                        (entry.lastrowid, accounts[0]["id"], "1234.56", "0", "fixture debit"),
                        (entry.lastrowid, accounts[1]["id"], "0", "1234.56", "fixture credit"),
                    ],
                )

                # A receipt and its valuation layer exercise inventory quantity
                # and exact value controls in the full-schema copy verifier.
                product = connection.execute(
                    """INSERT INTO stock_products
                       (sku,name_ar,category,product_type,uom,track_lot,track_expiry,
                        min_qty,purchase_price,sale_price)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    ("MIGRATION-TEST-001", "صنف اختبار النقل", "اختبار", "goods",
                     "قطعة", 0, 0, "0", "25.50", "40.00"),
                )
                warehouse = connection.execute(
                    """INSERT INTO stock_warehouses(code,name_ar,branch)
                       VALUES(?,?,?)""",
                    ("MIGRATION-TEST-WH", "مخزن اختبار النقل", ""),
                )
                movement = connection.execute(
                    """INSERT INTO stock_movements
                       (movement_no,movement_type,product_id,target_warehouse_id,
                        quantity,unit_cost,total_cost,movement_date,notes,created_by)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    ("MIGRATION-TEST-MOVE-001", "receipt", product.lastrowid,
                     warehouse.lastrowid, "10", "25.50", "255.00",
                     "2026-01-15", "Disposable migration rehearsal receipt", admin["id"]),
                )
                connection.execute(
                    """INSERT INTO stock_valuation_layers
                       (product_id,warehouse_id,source_movement_id,qty_in,
                        qty_remaining,unit_cost,value_remaining)
                       VALUES(?,?,?,?,?,?,?)""",
                    (product.lastrowid, warehouse.lastrowid, movement.lastrowid,
                     "10", "10", "25.50", "255.00"),
                )
                connection.commit()
            finally:
                connection.close()
        result = inspect_database(nexus_app.DB_PATH, include_row_counts=False)
        if database_output:
            output_path = Path(database_output).expanduser().resolve()
            output_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(nexus_app.DB_PATH, output_path)
            result["database_copy"] = str(output_path)
        result["source"] = {
            "kind": "fresh_initialized_application_schema",
            "database_engine": "sqlite",
            "business_data_included": False,
            "database_path": "temporary-only",
        }
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Export the fully initialized NEXUS SQLite schema from a fresh temporary DB."
    )
    parser.add_argument(
        "--output",
        help="Optional JSON output file. If omitted, write the schema to stdout.",
    )
    parser.add_argument(
        "--database-output",
        help="Optional path for a copy of the freshly initialized SQLite database.",
    )
    parser.add_argument(
        "--seed-representative-data",
        action="store_true",
        help="Insert a balanced journal fixture into the disposable database only.",
    )
    args = parser.parse_args(argv)

    result = build_initialized_schema(
        database_output=args.database_output,
        seed_representative_data=args.seed_representative_data,
    )
    encoded = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        output_path = Path(args.output).expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(encoded, encoding="utf-8")
        print(json.dumps({
            "output": str(output_path),
            "summary": result["summary"],
            "foreign_key_violations": len(result.get("foreign_key_violations", [])),
            "database_copy": result.get("database_copy"),
        }, ensure_ascii=False))
    else:
        # GitHub's Windows runner defaults stdout to cp1252; the schema contains
        # Arabic text, so emit the JSON as UTF-8 on every platform.
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        sys.stdout.write(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
