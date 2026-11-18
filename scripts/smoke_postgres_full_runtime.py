"""Exercise the complete Flask schema initializer against a generated PostgreSQL baseline.

This is a disposable CI smoke test, not the production runtime enablement switch.
It installs the reviewed baseline into a unique schema, runs every application
initializer through the PostgreSQL connection factory, verifies bootstrap data,
and removes only its own schema.
"""
from __future__ import annotations

import argparse
from datetime import date
import os
import re
import sys
import tempfile
import uuid
from pathlib import Path

import psycopg

# Direct script execution places scripts/ (not the repository root) on sys.path.
# Add the project root explicitly so root-level application modules resolve.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", required=True, type=Path)
    args = parser.parse_args()

    dsn = os.environ.get("NEXUS_TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        raise SystemExit("NEXUS_TEST_POSTGRES_DSN is required.")
    if not args.baseline.is_file():
        raise SystemExit(f"PostgreSQL baseline not found: {args.baseline}")

    schema = "admrail_runtime_smoke_" + uuid.uuid4().hex[:16]
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", schema):
        raise RuntimeError("Generated unsafe schema name.")

    with tempfile.TemporaryDirectory(prefix="admrail-pg-runtime-") as data_dir:
        backup_dir = Path(data_dir) / "backups"
        os.environ["NEXUS_SECRET_KEY"] = "postgres-runtime-smoke-secret-key-123456"
        os.environ["NEXUS_DEMO_MODE"] = "1"

        # Exercise the same guarded fresh-install entry point documented for
        # customer deployments, rather than duplicating its bootstrap logic.
        from scripts.initialize_postgres_fresh_install import main as fresh_install_main
        install_result = fresh_install_main([
            "--ddl", str(args.baseline),
            "--dsn", dsn,
            "--schema", schema,
            "--data-dir", str(Path(data_dir) / "data"),
            "--backup-dir", str(backup_dir),
        ])
        if install_result != 0:
            raise AssertionError(
                f"PostgreSQL fresh-install initializer returned {install_result}."
            )

        import app as nexus_app

        try:
            with nexus_app.app.app_context():
                connection = nexus_app.db()
                try:
                    user_count = connection.execute(
                        "SELECT COUNT(*) FROM users"
                    ).fetchone()[0]
                    account_count = connection.execute(
                        "SELECT COUNT(*) FROM accounts"
                    ).fetchone()[0]
                    settings_count = connection.execute(
                        "SELECT COUNT(*) FROM company_settings"
                    ).fetchone()[0]
                    if user_count < 1 or account_count < 1 or settings_count < 1:
                        raise AssertionError(
                            "PostgreSQL bootstrap did not seed the admin, chart of "
                            f"accounts, and company settings: users={user_count}, "
                            f"accounts={account_count}, settings={settings_count}"
                        )
                    # The three bootstrap verification SELECTs above open a
                    # psycopg transaction (unlike SQLite's default SELECT
                    # behavior). End that caller-owned read transaction before
                    # testing that the dashboard helper closes a transaction
                    # it starts itself.
                    connection.commit()
                    # Exercise a real dashboard read on the initialized PostgreSQL
                    # schema. This covers transaction-state handling and the query
                    # layer, not only DDL/bootstrap and the login route.
                    from datetime import date
                    from management_dashboard import cached_management_snapshot
                    from db_compat import transaction_is_active

                    today = date.today()
                    period_start = today.replace(day=1)
                    dashboard_snapshot = cached_management_snapshot(
                        connection, period_start, today
                    )
                    if not isinstance(dashboard_snapshot, dict) or not dashboard_snapshot:
                        raise AssertionError(
                            "PostgreSQL management dashboard returned an empty snapshot."
                        )
                    if transaction_is_active(connection):
                        raise AssertionError(
                            "PostgreSQL dashboard snapshot left an open transaction."
                        )
                    # Exercise one real inventory-backed sales posting against the
                    # fully initialized PostgreSQL application schema. This verifies
                    # operational SQL, generated IDs, stock valuation locks, and both
                    # revenue/COGS journals rather than only schema bootstrap.
                    from decimal import Decimal
                    from db_compat import insert_and_get_id
                    from db_sql import execute_compat
                    import sales

                    customer_id = insert_and_get_id(
                        connection,
                        "INSERT INTO customers(code,name,credit_limit) VALUES(?,?,?)",
                        ("PG-SMOKE-CUSTOMER", "عميل اختبار PostgreSQL", "1000000"),
                    )
                    warehouse_id = insert_and_get_id(
                        connection,
                        "INSERT INTO stock_warehouses(code,name_ar) VALUES(?,?)",
                        ("PG-SMOKE-WH", "مخزن اختبار PostgreSQL"),
                    )
                    product_id = insert_and_get_id(
                        connection,
                        "INSERT INTO stock_products(sku,name_ar) VALUES(?,?)",
                        ("PG-SMOKE-SKU", "صنف اختبار PostgreSQL"),
                    )
                    receipt_id = insert_and_get_id(
                        connection,
                        """INSERT INTO stock_movements(
                               movement_no,movement_type,product_id,target_warehouse_id,
                               quantity,unit_cost,total_cost,movement_date
                           ) VALUES(?, 'receipt', ?, ?, ?, ?, ?, ?)""",
                        ("PG-SMOKE-RECEIPT", product_id, warehouse_id,
                         "10", "5", "50", today.isoformat()),
                    )
                    insert_and_get_id(
                        connection,
                        """INSERT INTO stock_valuation_layers(
                               product_id,warehouse_id,source_movement_id,
                               qty_in,qty_remaining,unit_cost,value_remaining
                           ) VALUES(?,?,?,?,?,?,?)""",
                        (product_id, warehouse_id, receipt_id, "10", "10", "5", "50"),
                    )
                    admin_id = connection.execute(
                        "SELECT id FROM users WHERE username='admin'"
                    ).fetchone()["id"]
                    invoice = sales.post_invoice(
                        connection,
                        customer_id,
                        today.isoformat(),
                        warehouse_id,
                        [{"product_id": product_id, "quantity": "2", "unit_price": "20"}],
                        Decimal("0"),
                        created_by=admin_id,
                    )
                    connection.commit()

                    posted_invoice = execute_compat(
                        connection,
                        """SELECT total_amount,journal_entry_id,cogs_journal_entry_id
                             FROM sales_invoices WHERE id=?""",
                        (invoice["invoice_id"],),
                    ).fetchone()
                    remaining_layer = execute_compat(
                        connection,
                        """SELECT qty_remaining,value_remaining
                             FROM stock_valuation_layers WHERE product_id=? AND warehouse_id=?""",
                        (product_id, warehouse_id),
                    ).fetchone()
                    sales_observed = {
                        "invoice": dict(posted_invoice) if posted_invoice else None,
                        "returned_total_cost": str(invoice.get("total_cost")),
                        "remaining_layer": dict(remaining_layer) if remaining_layer else None,
                    }
                    if (
                        not posted_invoice
                        or not posted_invoice["journal_entry_id"]
                        or not posted_invoice["cogs_journal_entry_id"]
                        or Decimal(str(posted_invoice["total_amount"])) != Decimal("40.00")
                        or Decimal(str(invoice["total_cost"])) != Decimal("10.00")
                        or not remaining_layer
                        or Decimal(str(remaining_layer["qty_remaining"])) != Decimal("8")
                        or Decimal(str(remaining_layer["value_remaining"])) != Decimal("40")
                    ):
                        raise AssertionError(
                            "PostgreSQL sales workflow did not reconcile invoice, COGS, "
                            f"and remaining inventory valuation. Observed={sales_observed!r}"
                        )
                    sales_flow = {
                        "invoice_id": invoice["invoice_id"],
                        "journal_entry_id": posted_invoice["journal_entry_id"],
                        "cogs_journal_entry_id": posted_invoice["cogs_journal_entry_id"],
                        "total": str(posted_invoice["total_amount"]),
                        "cost": str(invoice["total_cost"]),
                    }
                    # Exercise a supplier expense bill through the real
                    # purchasing posting engine and verify its AP journal.
                    import purchases

                    supplier_id = insert_and_get_id(
                        connection,
                        "INSERT INTO suppliers(code,name) VALUES(?,?)",
                        ("PG-SMOKE-SUPPLIER", "مورد اختبار PostgreSQL"),
                    )
                    expense_account = execute_compat(
                        connection,
                        """SELECT id FROM accounts
                             WHERE account_type='expense' AND is_group=0 AND active=1
                             ORDER BY code LIMIT 1""",
                    ).fetchone()
                    if not expense_account:
                        raise AssertionError("PostgreSQL chart has no active detail expense account.")
                    bill_id = insert_and_get_id(
                        connection,
                        """INSERT INTO supplier_bills(
                               bill_no,supplier_id,bill_date,supplier_invoice_no,
                               subtotal,total_amount,status,bill_kind,expense_account_id
                           ) VALUES(?,?,?,?,?,?,'approved','expense',?)""",
                        ("PG-SMOKE-BILL", supplier_id, today.isoformat(),
                         "PG-SMOKE-SUPPLIER-INVOICE", "25.50", "25.50",
                         expense_account["id"]),
                    )
                    purchase_journal_id = purchases.post_supplier_bill(
                        connection, bill_id, admin_id
                    )
                    connection.commit()
                    posted_bill = execute_compat(
                        connection,
                        "SELECT status,journal_entry_id FROM supplier_bills WHERE id=?",
                        (bill_id,),
                    ).fetchone()
                    purchase_totals = execute_compat(
                        connection,
                        """SELECT SUM(CAST(debit AS NUMERIC)) AS debit,
                                  SUM(CAST(credit AS NUMERIC)) AS credit
                             FROM journal_lines WHERE entry_id=?""",
                        (purchase_journal_id,),
                    ).fetchone()
                    if (
                        not posted_bill
                        or posted_bill["status"] != "posted"
                        or posted_bill["journal_entry_id"] != purchase_journal_id
                        or Decimal(str(purchase_totals["debit"])) != Decimal("25.50")
                        or Decimal(str(purchase_totals["credit"])) != Decimal("25.50")
                    ):
                        raise AssertionError(
                            "PostgreSQL supplier bill did not post a balanced AP journal."
                        )
                    purchase_flow = {
                        "bill_id": bill_id,
                        "journal_entry_id": purchase_journal_id,
                        "total": "25.50",
                    }
                finally:
                    connection.close()

            response = nexus_app.app.test_client().get("/login")
            if response.status_code != 200:
                raise AssertionError(
                    f"Flask login page failed after PostgreSQL initialization: "
                    f"HTTP {response.status_code}"
                )

            print(
                "POSTGRES_FULL_APP_INITIALIZATION=PASS "
                f"schema={schema} users={user_count} accounts={account_count} "
                f"company_settings={settings_count} login_http={response.status_code} "
                f"sales_invoice={sales_flow['invoice_id']} sales_total={sales_flow['total']} "
                f"sales_cost={sales_flow['cost']} "
                f"supplier_bill={purchase_flow['bill_id']} "
                f"supplier_bill_total={purchase_flow['total']}"
            )
            return 0
        finally:
            # All application connections are request/app-context scoped and
            # must be closed before removing this isolated test schema.
            with psycopg.connect(dsn, autocommit=True) as connection:
                connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


if __name__ == "__main__":
    raise SystemExit(main())
