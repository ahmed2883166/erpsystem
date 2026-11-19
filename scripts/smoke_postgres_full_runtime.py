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
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
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
        from database_backend import require_supported_runtime_backend

        # Exercise the real backend selector and connection factory. The
        # fresh-install command has already scoped NEXUS_POSTGRES_DSN to this
        # disposable schema and selected NEXUS_DB_BACKEND=postgresql.
        selected_backend = require_supported_runtime_backend()
        if selected_backend != "postgresql":
            raise AssertionError(
                f"Runtime selector chose {selected_backend!r}, not PostgreSQL."
            )
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
                    # The installer intentionally marks its first admin for a forced
                    # password change. This smoke test injects an authenticated session
                    # directly (it does not test the login/password-change flow), so mark
                    # that disposable CI admin as already onboarded before exercising
                    # protected workforce routes. This update is confined to the
                    # temporary PostgreSQL schema created above.
                    execute_compat(
                        connection,
                        "UPDATE users SET must_change_password=0 WHERE id=?",
                        (admin_id,),
                    )
                    connection.commit()
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

                    # Exercise the real fixed-asset depreciation posting helper
                    # on the same initialized PostgreSQL application schema.
                    import fixed_assets

                    asset_accounts = execute_compat(
                        connection,
                        """SELECT id FROM accounts
                             WHERE account_type='asset' AND is_group=0 AND active=1
                             ORDER BY code LIMIT 2""",
                    ).fetchall()
                    if len(asset_accounts) < 2:
                        raise AssertionError(
                            "PostgreSQL chart has fewer than two active detail asset accounts."
                        )
                    asset_expense_account = execute_compat(
                        connection,
                        """SELECT id FROM accounts
                             WHERE account_type='expense' AND is_group=0 AND active=1
                             ORDER BY code LIMIT 1""",
                    ).fetchone()
                    if not asset_expense_account:
                        raise AssertionError(
                            "PostgreSQL chart has no active detail expense account for depreciation."
                        )
                    asset_ready_date = today.replace(day=1).isoformat()
                    asset_id = insert_and_get_id(
                        connection,
                        """INSERT INTO fixed_assets(
                               asset_code,asset_name,category,purchase_date,ready_date,
                               cost,residual_value,useful_life_months,depreciation_method,
                               annual_rate,asset_account_id,accum_account_id,
                               expense_account_id,status,created_by
                           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'active',?)""",
                        (
                            "PG-SMOKE-AST", "أصل اختبار PostgreSQL", "اختبار",
                            asset_ready_date, asset_ready_date, "1200", "0", 12,
                            "straight_line", "0", asset_accounts[0]["id"],
                            asset_accounts[1]["id"], asset_expense_account["id"], admin_id,
                        ),
                    )
                    asset = execute_compat(
                        connection, "SELECT * FROM fixed_assets WHERE id=?", (asset_id,)
                    ).fetchone()
                    depreciation_amount, depreciation_journal_id = (
                        fixed_assets._post_asset_depreciation(
                            connection, asset, today.strftime("%Y-%m"), admin_id
                        )
                    )
                    connection.commit()
                    depreciation_totals = execute_compat(
                        connection,
                        """SELECT SUM(CAST(debit AS NUMERIC)) AS debit,
                                  SUM(CAST(credit AS NUMERIC)) AS credit
                             FROM journal_lines WHERE entry_id=?""",
                        (depreciation_journal_id,),
                    ).fetchone()
                    depreciation_record = execute_compat(
                        connection,
                        "SELECT amount,journal_entry_id FROM asset_depreciations WHERE asset_id=? AND period=?",
                        (asset_id, today.strftime("%Y-%m")),
                    ).fetchone()
                    if (
                        Decimal(str(depreciation_amount)) != Decimal("100.00")
                        or not depreciation_record
                        or depreciation_record["journal_entry_id"] != depreciation_journal_id
                        or Decimal(str(depreciation_totals["debit"])) != Decimal("100.00")
                        or Decimal(str(depreciation_totals["credit"])) != Decimal("100.00")
                    ):
                        raise AssertionError(
                            "PostgreSQL fixed-asset depreciation did not create the expected "
                            "asset schedule and balanced journal."
                        )
                    asset_flow = {
                        "asset_id": asset_id,
                        "journal_entry_id": depreciation_journal_id,
                        "amount": str(depreciation_amount),
                    }

                    # Exercise the real bank statement matching helpers against the
                    # full PostgreSQL application schema. The fixture uses a dedicated
                    # asset account and a disposable journal entry.
                    import banking
                    from accounting_core import post_journal

                    bank_account = execute_compat(
                        connection,
                        """SELECT a.id FROM accounts a
                             WHERE a.account_type='asset' AND a.is_group=0 AND a.active=1
                               AND NOT EXISTS (
                                   SELECT 1 FROM cash_accounts ca WHERE ca.account_id=a.id
                               )
                             ORDER BY a.code LIMIT 1""",
                    ).fetchone()
                    if not bank_account:
                        raise AssertionError("PostgreSQL chart has no unused asset account for bank reconciliation smoke.")
                    base_currency_row = execute_compat(
                        connection,
                        "SELECT setting_value FROM company_settings WHERE setting_key='base_currency'",
                    ).fetchone()
                    base_currency = base_currency_row["setting_value"] if base_currency_row else "EGP"
                    bank_account_id = int(bank_account["id"])
                    insert_and_get_id(
                        connection,
                        """INSERT INTO cash_accounts(
                               kind,name_ar,account_id,currency_code,bank_name,opening_date,created_by
                           ) VALUES('bank',?,?,?,?,?,?)""",
                        ("بنك اختبار PostgreSQL", bank_account_id, base_currency,
                         "PG Smoke Bank", today.isoformat(), admin_id),
                    )
                    bank_journal_id = post_journal(
                        connection, today.isoformat(), "PG-SMOKE-BANK-RECON",
                        "اختبار مطابقة كشف بنك PostgreSQL",
                        [
                            (bank_account_id, Decimal("75.00"), Decimal("0"), "إيداع اختبار"),
                            (int(expense_account["id"]), Decimal("0"), Decimal("75.00"), "مقابل إيداع اختبار"),
                        ],
                        admin_id,
                    )
                    bank_transaction_id = insert_and_get_id(
                        connection,
                        """INSERT INTO bank_transactions(
                               transaction_no,transaction_date,bank_account_id,transaction_type,
                               amount,counter_account_id,description,bank_reference,journal_entry_id,
                               reconciled,created_by
                           ) VALUES(?,?,?,'deposit',?,?,?,?,?,0,?)""",
                        ("PG-SMOKE-BANK-TX", today.isoformat(), bank_account_id, "75.00",
                         int(expense_account["id"]), "إيداع اختبار", "PG-SMOKE-REF",
                         bank_journal_id, admin_id),
                    )
                    bank_journal_line = execute_compat(
                        connection,
                        """SELECT id FROM journal_lines
                             WHERE entry_id=? AND account_id=? AND debit='75.00'
                             ORDER BY id LIMIT 1""",
                        (bank_journal_id, bank_account_id),
                    ).fetchone()
                    if not bank_journal_line:
                        # PostgreSQL numeric columns may be returned in a normalized
                        # representation; compare numerically instead of relying on text.
                        bank_journal_line = execute_compat(
                            connection,
                            """SELECT id FROM journal_lines
                                 WHERE entry_id=? AND account_id=? AND CAST(debit AS NUMERIC)=75
                                 ORDER BY id LIMIT 1""",
                            (bank_journal_id, bank_account_id),
                        ).fetchone()
                    if not bank_journal_line:
                        raise AssertionError("Bank reconciliation smoke journal has no bank-side journal line.")
                    bank_statement_line_id = insert_and_get_id(
                        connection,
                        """INSERT INTO bank_statement_lines(
                               bank_account_id,statement_date,signed_amount,description,
                               bank_reference,created_by
                           ) VALUES(?,?,?,?,?,?)""",
                        (bank_account_id, today.isoformat(), "75.00",
                         "إيداع اختبار", "PG-SMOKE-REF", admin_id),
                    )
                    banking.save_statement_line_match(
                        connection, bank_statement_line_id, bank_journal_line["id"],
                        today.isoformat(), admin_id,
                    )
                    if not banking.refresh_bank_transaction_reconciled(connection, bank_journal_id):
                        raise AssertionError("PostgreSQL bank reconciliation did not refresh the transaction.")
                    reconciled_bank = execute_compat(
                        connection, "SELECT reconciled FROM bank_transactions WHERE id=?",
                        (bank_transaction_id,),
                    ).fetchone()
                    if not reconciled_bank or int(reconciled_bank["reconciled"]) != 1:
                        raise AssertionError("A fully matched PostgreSQL bank transaction was not marked reconciled.")
                    duplicate_match_rejected = False
                    try:
                        banking.save_statement_line_match(
                            connection, bank_statement_line_id, bank_journal_line["id"],
                            today.isoformat(), admin_id,
                        )
                    except ValueError:
                        duplicate_match_rejected = True
                    if not duplicate_match_rejected:
                        raise AssertionError("PostgreSQL bank reconciliation allowed a duplicate statement match.")
                    bank_flow = {
                        "transaction_id": bank_transaction_id,
                        "journal_entry_id": bank_journal_id,
                        "statement_line_id": bank_statement_line_id,
                    }

                    # Exercise Egypt and Saudi VAT preparation against the same
                    # full-app PostgreSQL schema. These tax values belong only to
                    # this disposable smoke database; no production document is touched.
                    import tax_center
                    previous_tax_country = execute_compat(
                        connection,
                        "SELECT setting_value FROM company_settings WHERE setting_key='tax_country'",
                    ).fetchone()
                    previous_tax_number = execute_compat(
                        connection,
                        "SELECT setting_value FROM company_settings WHERE setting_key='tax_number'",
                    ).fetchone()
                    execute_compat(
                        connection,
                        """INSERT INTO company_settings(setting_key,setting_value)
                             VALUES('tax_country','EG')
                             ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value""",
                    )
                    execute_compat(
                        connection,
                        """INSERT INTO company_settings(setting_key,setting_value)
                             VALUES('tax_number','123456789')
                             ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value""",
                    )
                    execute_compat(
                        connection,
                        """UPDATE sales_invoices
                              SET subtotal='40.00',tax_rate='14',tax_amount='5.60',
                                  total_amount='45.60',tax_reference='PG-SMOKE-SALE'
                            WHERE id=?""",
                        (invoice["invoice_id"],),
                    )
                    execute_compat(
                        connection,
                        """UPDATE supplier_bills
                              SET subtotal='25.50',tax_rate='14',tax_amount='3.57',
                                  total_amount='29.07',tax_reference='PG-SMOKE-PURCHASE',
                                  input_vat_deductible_percent='50'
                            WHERE id=?""",
                        (bill_id,),
                    )
                    egypt_tax_snapshot = tax_center.build_tax_report_snapshot(
                        connection, today.strftime("%Y-%m")
                    )
                    egypt_tax = egypt_tax_snapshot["summary"]
                    if (
                        egypt_tax_snapshot["tax_profile"]["country_code"] != "EG"
                        or Decimal(str(egypt_tax_snapshot["tax_profile"]["standard_rate"])) != Decimal("14")
                        or Decimal(str(egypt_tax["output_vat"])) != Decimal("5.60")
                        or Decimal(str(egypt_tax["input_vat"])) != Decimal("1.79")
                        or Decimal(str(egypt_tax["net_vat"])) != Decimal("3.81")
                    ):
                        raise AssertionError(
                            "Egypt PostgreSQL VAT snapshot did not reconcile output/input VAT and deductible percentage."
                        )
                    execute_compat(
                        connection,
                        """UPDATE company_settings SET setting_value='SA'
                             WHERE setting_key='tax_country'""",
                    )
                    saudi_tax_snapshot = tax_center.build_tax_report_snapshot(
                        connection, today.strftime("%Y-%m")
                    )
                    saudi_preparation = saudi_tax_snapshot.get("saudi_preparation")
                    if (
                        saudi_tax_snapshot["tax_profile"]["country_code"] != "SA"
                        or Decimal(str(saudi_tax_snapshot["tax_profile"]["standard_rate"])) != Decimal("15")
                        or saudi_preparation is None
                        or saudi_preparation.get("zakat_or_income_tax") is not None
                    ):
                        raise AssertionError(
                            "Saudi PostgreSQL tax workpaper did not expose the 15% VAT profile and accounting-only preparation."
                        )
                    if previous_tax_country:
                        execute_compat(
                            connection,
                            "UPDATE company_settings SET setting_value=? WHERE setting_key='tax_country'",
                            (previous_tax_country["setting_value"],),
                        )
                    if previous_tax_number:
                        execute_compat(
                            connection,
                            "UPDATE company_settings SET setting_value=? WHERE setting_key='tax_number'",
                            (previous_tax_number["setting_value"],),
                        )
                    tax_flow = {
                        "egypt_output_vat": str(egypt_tax["output_vat"]),
                        "egypt_deductible_input_vat": str(egypt_tax["input_vat"]),
                        "saudi_standard_rate": str(saudi_tax_snapshot["tax_profile"]["standard_rate"]),
                    }

                    # Run a complete maker-checker payroll lifecycle through the real
                    # Flask routes. All records remain inside this disposable PostgreSQL
                    # schema and use manual statutory inputs (zero deductions).
                    liability_account = execute_compat(
                        connection,
                        """SELECT id FROM accounts
                             WHERE account_type='liability' AND is_group=0 AND active=1
                             ORDER BY code LIMIT 1""",
                    ).fetchone()
                    if not liability_account:
                        raise AssertionError("PostgreSQL chart has no active detail liability account for payroll.")
                    liability_account_id = int(liability_account["id"])
                    approver_row = execute_compat(
                        connection,
                        """INSERT INTO users(
                               username,full_name,password_hash,role,created_at,must_change_password
                           )
                           SELECT 'pg-smoke-approver','مستخدم اعتماد اختبار',password_hash,
                                  'admin',CURRENT_TIMESTAMP,0
                             FROM users WHERE id=?
                           RETURNING id""",
                        (admin_id,),
                    ).fetchone()
                    if not approver_row:
                        raise AssertionError("Could not create the isolated PostgreSQL payroll approver.")
                    approver_id = int(approver_row["id"])
                    employee_id = insert_and_get_id(
                        connection,
                        """INSERT INTO employees(
                               employee_code,full_name,hire_date,basic_salary,insured_wage,
                               active,work_status
                           ) VALUES(?,?,?,?,?,1,'permanent')""",
                        ("PG-SMOKE-EMP", "موظف اختبار المرتبات PostgreSQL",
                         today.isoformat(), "1200.00", "1200.00"),
                    )
                    connection.commit()

                    client = nexus_app.app.test_client()
                    def set_session_user(user_id):
                        with client.session_transaction() as session_data:
                            # Change only the authenticated identity. Flask-WTF's
                            # session-bound CSRF secret must survive role switches,
                            # exactly as it does when a browser logs in/out without
                            # discarding the whole cookie session.
                            csrf_secret = session_data.get("csrf_token")
                            session_data.clear()
                            if csrf_secret:
                                session_data["csrf_token"] = csrf_secret
                            session_data["user_id"] = int(user_id)
                            session_data["role"] = "admin"

                    def post_with_csrf(path, *, data, follow_redirects=False):
                        # Exercise the same session-bound CSRF flow as a real browser:
                        # fetch a rendered form, then submit its injected token.
                        csrf_page = client.get("/hr/payroll")
                        if csrf_page.status_code != 200:
                            raise AssertionError(
                                "Could not load payroll form before PostgreSQL smoke POST: "
                                f"HTTP {csrf_page.status_code}."
                            )
                        csrf_match = re.search(
                            r'<input type="hidden" name="csrf_token" value="([^"]+)"',
                            csrf_page.get_data(as_text=True),
                        )
                        if not csrf_match:
                            raise AssertionError(
                                "Payroll page did not render the CSRF token required for POST."
                            )
                        # Rendering the form creates Flask-WTF's session secret and
                        # signed token. Submit that exact token; do not call
                        # generate_csrf() outside a request context, which would bind
                        # it to a different context/session than the test client's cookie.
                        with client.session_transaction() as session_data:
                            if not session_data.get("csrf_token"):
                                raise AssertionError(
                                    "Rendered payroll CSRF token has no persisted session secret."
                                )
                        payload = dict(data)
                        payload["csrf_token"] = csrf_match.group(1)
                        return client.post(
                            path, data=payload, follow_redirects=follow_redirects
                        )

                    payroll_period = today.strftime("%Y-%m")
                    set_session_user(admin_id)
                    create_response = post_with_csrf(
                        "/hr/payroll",
                        data={"period": payroll_period},
                        follow_redirects=False,
                    )
                    payroll_run = execute_compat(
                        connection,
                        "SELECT id,status,created_by,approval_status FROM payroll_runs WHERE period=?",
                        (payroll_period,),
                    ).fetchone()
                    with client.session_transaction() as session_data:
                        payroll_flashes = list(session_data.get("_flashes", []))
                    payroll_observed = {
                        "http_status": create_response.status_code,
                        "location": create_response.headers.get("Location"),
                        "run": dict(payroll_run) if payroll_run else None,
                        "flashes": payroll_flashes,
                    }
                    if (
                        create_response.status_code not in (302, 303)
                        or not payroll_run
                        or payroll_run["status"] != "draft"
                        or int(payroll_run["created_by"]) != int(admin_id)
                        or payroll_run["approval_status"] != "pending"
                    ):
                        raise AssertionError(
                            "PostgreSQL payroll draft creation did not preserve "
                            f"maker/pending approval state. Observed={payroll_observed!r}"
                        )
                    payroll_run_id = int(payroll_run["id"])
                    payroll_line = execute_compat(
                        connection,
                        "SELECT id,net_pay,gross_pay FROM payroll_lines WHERE run_id=? AND employee_id=?",
                        (payroll_run_id, employee_id),
                    ).fetchone()
                    if (
                        not payroll_line
                        or Decimal(str(payroll_line["gross_pay"])) != Decimal("1200.00")
                        or Decimal(str(payroll_line["net_pay"])) != Decimal("1200.00")
                    ):
                        raise AssertionError("PostgreSQL payroll draft did not calculate the expected manual-input net pay.")

                    payroll_accounts = {
                        "salary_expense_account": int(expense_account["id"]),
                        "employer_ins_expense_account": int(expense_account["id"]),
                        "salaries_payable_account": liability_account_id,
                        "tax_payable_account": liability_account_id,
                        "insurance_payable_account": liability_account_id,
                        "other_deductions_account": liability_account_id,
                        "advances_account": liability_account_id,
                    }
                    set_session_user(admin_id)
                    creator_post_response = post_with_csrf(
                        f"/hr/payroll/{payroll_run_id}/post",
                        data=payroll_accounts,
                        follow_redirects=False,
                    )
                    with client.session_transaction() as session_data:
                        creator_post_flashes = list(session_data.get("_flashes", []))
                    creator_post_state = execute_compat(
                        connection, "SELECT status,journal_entry_id FROM payroll_runs WHERE id=?",
                        (payroll_run_id,),
                    ).fetchone()
                    if (
                        creator_post_response.status_code not in (302, 303)
                        or not creator_post_state
                        or creator_post_state["status"] != "draft"
                        or creator_post_state["journal_entry_id"] is not None
                        or not any("لا يجوز" in str(message) for _, message in creator_post_flashes)
                    ):
                        raise AssertionError(
                            "PostgreSQL payroll did not reject creator self-approval. "
                            f"Observed={{'http_status': creator_post_response.status_code, "
                            f"'run': dict(creator_post_state) if creator_post_state else None, "
                            f"'flashes': creator_post_flashes!r}}"
                        )

                    set_session_user(approver_id)
                    approve_response = post_with_csrf(
                        f"/hr/payroll/{payroll_run_id}/post",
                        data=payroll_accounts,
                        follow_redirects=False,
                    )
                    with client.session_transaction() as session_data:
                        approval_flashes = list(session_data.get("_flashes", []))
                    posted_payroll = execute_compat(
                        connection,
                        "SELECT status,journal_entry_id,approved_by,approval_status FROM payroll_runs WHERE id=?",
                        (payroll_run_id,),
                    ).fetchone()
                    payroll_approval_observed = {
                        "http_status": approve_response.status_code,
                        "location": approve_response.headers.get("Location"),
                        "response_body": approve_response.get_data(as_text=True)[:3000],
                        "run": dict(posted_payroll) if posted_payroll else None,
                        "flashes": approval_flashes,
                    }
                    if (
                        approve_response.status_code not in (302, 303)
                        or not posted_payroll
                        or posted_payroll["status"] != "posted"
                        or int(posted_payroll["approved_by"]) != approver_id
                        or not posted_payroll["journal_entry_id"]
                    ):
                        raise AssertionError(
                            "PostgreSQL payroll maker-checker approval did not post under "
                            f"the separate approver. Observed={payroll_approval_observed!r}"
                        )
                    payroll_journal_id = int(posted_payroll["journal_entry_id"])
                    payroll_journal_totals = execute_compat(
                        connection,
                        """SELECT SUM(CAST(debit AS NUMERIC)) AS debit,
                                  SUM(CAST(credit AS NUMERIC)) AS credit
                             FROM journal_lines WHERE entry_id=?""",
                        (payroll_journal_id,),
                    ).fetchone()
                    if (
                        Decimal(str(payroll_journal_totals["debit"])) != Decimal("1200.00")
                        or Decimal(str(payroll_journal_totals["credit"])) != Decimal("1200.00")
                    ):
                        raise AssertionError("PostgreSQL payroll journal is not balanced at the expected 1200.00.")

                    # Duplicate posting must leave the original journal untouched.
                    duplicate_post_response = post_with_csrf(
                        f"/hr/payroll/{payroll_run_id}/post",
                        data=payroll_accounts,
                        follow_redirects=False,
                    )
                    duplicate_post_state = execute_compat(
                        connection, "SELECT status,journal_entry_id FROM payroll_runs WHERE id=?",
                        (payroll_run_id,),
                    ).fetchone()
                    if (
                        duplicate_post_response.status_code not in (302, 303)
                        or duplicate_post_state["status"] != "posted"
                        or int(duplicate_post_state["journal_entry_id"]) != payroll_journal_id
                        or execute_compat(
                            connection, "SELECT COUNT(*) AS n FROM journal_entries WHERE reference LIKE ?",
                            ("PAYROLL-" + payroll_period + "-RUN-" + str(payroll_run_id),),
                        ).fetchone()["n"] != 1
                    ):
                        raise AssertionError("PostgreSQL payroll duplicate posting created or changed a journal.")

                    net_payable = Decimal(str(payroll_line["net_pay"]))
                    set_session_user(approver_id)
                    post_with_csrf(
                        f"/hr/payroll/{payroll_run_id}/payment",
                        data={"amount": str(net_payable), "payment_date": today.isoformat(),
                              "cash_account_id": str(bank_account_id)},
                        follow_redirects=False,
                    )
                    if execute_compat(
                        connection, "SELECT COUNT(*) AS n FROM payroll_payments WHERE run_id=?",
                        (payroll_run_id,),
                    ).fetchone()["n"] != 0:
                        raise AssertionError("PostgreSQL payroll allowed the approver to pay the same payroll run.")

                    set_session_user(admin_id)
                    payment_response = post_with_csrf(
                        f"/hr/payroll/{payroll_run_id}/payment",
                        data={"amount": str(net_payable), "payment_date": today.isoformat(),
                              "cash_account_id": str(bank_account_id)},
                        follow_redirects=False,
                    )
                    payment_row = execute_compat(
                        connection,
                        "SELECT amount,journal_entry_id FROM payroll_payments WHERE run_id=?",
                        (payroll_run_id,),
                    ).fetchone()
                    if (
                        payment_response.status_code not in (302, 303)
                        or not payment_row
                        or Decimal(str(payment_row["amount"])) != net_payable
                    ):
                        raise AssertionError("PostgreSQL payroll payment by the original creator did not complete.")
                    payment_journal_totals = execute_compat(
                        connection,
                        """SELECT SUM(CAST(debit AS NUMERIC)) AS debit,
                                  SUM(CAST(credit AS NUMERIC)) AS credit
                             FROM journal_lines WHERE entry_id=?""",
                        (payment_row["journal_entry_id"],),
                    ).fetchone()
                    if (
                        Decimal(str(payment_journal_totals["debit"])) != net_payable
                        or Decimal(str(payment_journal_totals["credit"])) != net_payable
                    ):
                        raise AssertionError("PostgreSQL payroll payment journal is not balanced.")
                    post_with_csrf(
                        f"/hr/payroll/{payroll_run_id}/payment",
                        data={"amount": "0.01", "payment_date": today.isoformat(),
                              "cash_account_id": str(bank_account_id)},
                        follow_redirects=False,
                    )
                    final_payment_state = execute_compat(
                        connection,
                        "SELECT COUNT(*) AS n,COALESCE(SUM(CAST(amount AS NUMERIC)),0) AS total FROM payroll_payments WHERE run_id=?",
                        (payroll_run_id,),
                    ).fetchone()
                    if (
                        final_payment_state["n"] != 1
                        or Decimal(str(final_payment_state["total"])) != net_payable
                    ):
                        raise AssertionError("PostgreSQL payroll accepted an overpayment or duplicated a payment.")
                    connection.commit()
                    payroll_flow = {
                        "run_id": payroll_run_id,
                        "journal_entry_id": payroll_journal_id,
                        "payment_journal_id": int(payment_row["journal_entry_id"]),
                        "net_payable": str(net_payable),
                    }

                    # Exercise 30 real PostgreSQL connections at once: 15 independent
                    # journal writers and 15 report readers. This is a database-client
                    # concurrency acceptance test, not an HTTP-session simulation.
                    from db_compat import begin_write_transaction
                    start_barrier = threading.Barrier(30)
                    write_latencies = []
                    read_latencies = []

                    def pg_writer(writer_number):
                        with nexus_app.app.app_context():
                            worker_connection = nexus_app.db()
                            try:
                                start_barrier.wait(timeout=30)
                                durations = []
                                for sequence in range(10):
                                    started = time.perf_counter()
                                    begin_write_transaction(worker_connection)
                                    try:
                                        post_journal(
                                            worker_connection,
                                            today.isoformat(),
                                            f"PG-CONC-{writer_number:02d}-{sequence:03d}",
                                            "اختبار تزامن PostgreSQL",
                                            [
                                                (int(expense_account["id"]), Decimal("1.00"), Decimal("0"), "مدين اختبار التزامن"),
                                                (liability_account_id, Decimal("0"), Decimal("1.00"), "دائن اختبار التزامن"),
                                            ],
                                            admin_id,
                                        )
                                        worker_connection.commit()
                                    except Exception:
                                        worker_connection.rollback()
                                        raise
                                    durations.append(time.perf_counter() - started)
                                write_latencies.extend(durations)
                                return len(durations)
                            finally:
                                worker_connection.close()

                    def pg_reader(reader_number):
                        with nexus_app.app.app_context():
                            worker_connection = nexus_app.db()
                            try:
                                start_barrier.wait(timeout=30)
                                durations = []
                                for _ in range(30):
                                    started = time.perf_counter()
                                    totals = execute_compat(
                                        worker_connection,
                                        """SELECT COUNT(*) AS entries,
                                                  COALESCE(SUM((SELECT COUNT(*) FROM journal_lines jl
                                                      WHERE jl.entry_id=je.id)),0) AS lines
                                             FROM journal_entries je""",
                                    ).fetchone()
                                    account_totals = execute_compat(
                                        worker_connection,
                                        """SELECT account_id,
                                                  SUM(CAST(debit AS NUMERIC)) AS debit,
                                                  SUM(CAST(credit AS NUMERIC)) AS credit
                                             FROM journal_lines
                                            WHERE account_id IN (?,?)
                                            GROUP BY account_id""",
                                        (int(expense_account["id"]), liability_account_id),
                                    ).fetchall()
                                    if totals["entries"] < 1 or totals["lines"] < 2 or not account_totals:
                                        raise AssertionError(f"PostgreSQL report reader {reader_number} observed incomplete ledger data.")
                                    worker_connection.commit()
                                    durations.append(time.perf_counter() - started)
                                read_latencies.extend(durations)
                                return len(durations)
                            finally:
                                worker_connection.close()

                    started = time.perf_counter()
                    with ThreadPoolExecutor(max_workers=30) as pool:
                        futures = [
                            pool.submit(pg_writer, number) for number in range(1, 16)
                        ] + [
                            pool.submit(pg_reader, number) for number in range(1, 16)
                        ]
                        completed_counts = [future.result(timeout=180) for future in futures]
                    concurrency_elapsed = time.perf_counter() - started
                    expected_concurrency_entries = 150
                    concurrency_state = execute_compat(
                        connection,
                        """SELECT COUNT(DISTINCT je.id) AS entries,
                                  COUNT(jl.id) AS lines,
                                  SUM(CAST(jl.debit AS NUMERIC)) AS debit,
                                  SUM(CAST(jl.credit AS NUMERIC)) AS credit
                             FROM journal_entries je
                             JOIN journal_lines jl ON jl.entry_id=je.id
                            WHERE LEFT(je.reference, 8) = 'PG-CONC-'""",
                    ).fetchone()
                    if (
                        sum(completed_counts) != 600
                        or len(write_latencies) != 150
                        or len(read_latencies) != 450
                        or int(concurrency_state["entries"]) != expected_concurrency_entries
                        or int(concurrency_state["lines"]) != expected_concurrency_entries * 2
                        or Decimal(str(concurrency_state["debit"])) != Decimal("150.00")
                        or Decimal(str(concurrency_state["credit"])) != Decimal("150.00")
                    ):
                        raise AssertionError("PostgreSQL 30-client concurrency workload lost, duplicated, or unbalanced ledger activity.")
                    write_p95 = sorted(write_latencies)[int(len(write_latencies) * 0.95) - 1]
                    read_p95 = sorted(read_latencies)[int(len(read_latencies) * 0.95) - 1]
                    concurrency_flow = {
                        "entries": expected_concurrency_entries,
                        "read_queries": len(read_latencies),
                        "elapsed_seconds": round(concurrency_elapsed, 3),
                        "write_p95_seconds": round(write_p95, 4),
                        "read_p95_seconds": round(read_p95, 4),
                    }
                finally:
                    connection.close()

            # Re-running the documented installer against the same baseline
            # must be idempotent and must preserve transactions already entered.
            repeat_install_result = fresh_install_main([
                "--ddl", str(args.baseline),
                "--dsn", dsn,
                "--schema", schema,
                "--data-dir", str(Path(data_dir) / "data"),
                "--backup-dir", str(backup_dir),
            ])
            if repeat_install_result != 0:
                raise AssertionError(
                    f"Repeated PostgreSQL bootstrap returned {repeat_install_result}."
                )
            with nexus_app.app.app_context():
                verification_connection = nexus_app.db()
                try:
                    invoice_after_repeat = execute_compat(
                        verification_connection,
                        "SELECT journal_entry_id,cogs_journal_entry_id FROM sales_invoices WHERE id=?",
                        (sales_flow["invoice_id"],),
                    ).fetchone()
                    bill_after_repeat = execute_compat(
                        verification_connection,
                        "SELECT status,journal_entry_id FROM supplier_bills WHERE id=?",
                        (purchase_flow["bill_id"],),
                    ).fetchone()
                    asset_after_repeat = execute_compat(
                        verification_connection,
                        """SELECT d.amount,d.journal_entry_id,a.status
                             FROM asset_depreciations d
                             JOIN fixed_assets a ON a.id=d.asset_id
                            WHERE d.asset_id=? AND d.period=?""",
                        (asset_flow["asset_id"], today.strftime("%Y-%m")),
                    ).fetchone()
                    if (
                        not invoice_after_repeat
                        or not invoice_after_repeat["journal_entry_id"]
                        or not invoice_after_repeat["cogs_journal_entry_id"]
                        or not bill_after_repeat
                        or bill_after_repeat["status"] != "posted"
                        or bill_after_repeat["journal_entry_id"] != purchase_flow["journal_entry_id"]
                        or not asset_after_repeat
                        or asset_after_repeat["status"] != "active"
                        or asset_after_repeat["journal_entry_id"] != asset_flow["journal_entry_id"]
                        or Decimal(str(asset_after_repeat["amount"])) != Decimal("100.00")
                    ):
                        raise AssertionError(
                            "Repeated PostgreSQL bootstrap changed or lost posted "
                            "sales, purchase, or fixed-asset records."
                        )
                    verification_connection.commit()
                finally:
                    verification_connection.close()

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
                f"supplier_bill_total={purchase_flow['total']} "
                f"asset_depreciation={asset_flow['amount']} "
                f"asset_journal={asset_flow['journal_entry_id']}"
            )
            return 0
        finally:
            # All application connections are request/app-context scoped and
            # must be closed before removing this isolated test schema.
            with psycopg.connect(dsn, autocommit=True) as connection:
                connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


if __name__ == "__main__":
    raise SystemExit(main())
