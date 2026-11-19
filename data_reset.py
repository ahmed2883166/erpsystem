"""Guarded company-data reset that preserves NEXUS system configuration and access."""

from db_compat import database_dialect, table_names, begin_write_transaction

# Fail-closed policy: only these explicitly reviewed operational tables may be
# cleared. Any table added by a future feature must be classified here first.
PRESERVED_TABLES = frozenset({
    "users", "accounts", "company_settings", "currencies", "tax_rates", "audit_log",
    "activity_profiles", "cash_flow_account_mappings", "management_account_roles",
    "payroll_rule_sets", "payroll_tax_brackets", "payroll_tax_tier_brackets",
    "fx_exchange_rates", "licenses", "trial_devices",
})

DELETE_TABLES = frozenset({
    "accounting_periods",
    "asset_depreciations",
    "asset_disposals",
    "attendance_records",
    "attendance_records_rebuilt",
    "bank_transactions",
    "branches",
    "budget_audit_log",
    "budget_lines",
    "budget_monthly_lines",
    "budget_plans",
    "cost_centers",
    "custody_expenses",
    "customer_advance_allocations",
    "customer_advance_receipts",
    "customer_advance_refunds",
    "customer_advance_reversals",
    "customer_price_rules",
    "customer_receipt_allocations",
    "customer_receipt_batches",
    "customer_receipts",
    "customer_refunds",
    "customers",
    "delivery_note_line_lots",
    "delivery_note_lines",
    "delivery_notes",
    "employee_custodies",
    "employee_grant_lines",
    "employee_grant_payments",
    "employee_grant_types",
    "employee_grants",
    "employee_leave_balances",
    "employee_leave_requests",
    "employee_loan_installments",
    "employee_loans",
    "employee_pay_items",
    "employee_penalties",
    "employee_salary_history",
    "employees",
    "financial_year_journal_history",
    "financial_years",
    "fixed_asset_journal_lines",
    "fixed_asset_supplier_bill_links",
    "fixed_assets",
    "friday_allowance_batches",
    "friday_allowance_lines",
    "fx_revaluations",
    "fx_settlements",
    "fx_transactions",
    "goods_receipt_lines",
    "goods_receipts",
    "journal_entries",
    "journal_lines",
    "journal_lines_decimal_new",
    "leave_types",
    "login_attempts",
    "opening_balance_import_refs",
    "payroll_incentive_rules",
    "payroll_line_items",
    "payroll_lines",
    "payroll_loan_allocations",
    "payroll_payment_allocations",
    "payroll_payments",
    "payroll_reversals",
    "payroll_runs",
    "payroll_runs_rebuilt",
    "payroll_statutory_payments",
    "projects",
    "purchase_integrity_issues",
    "purchase_order_lines",
    "purchase_orders",
    "quotation_lines",
    "quotations",
    "salary_change_batches",
    "sales_credit_note_line_lots",
    "sales_credit_note_lines",
    "sales_credit_notes",
    "sales_invoice_line_lots",
    "sales_invoice_lines",
    "sales_invoices",
    "sales_order_lines",
    "sales_orders",
    "salespersons",
    "stock_categories",
    "stock_locations",
    "stock_lots",
    "stock_movements",
    "stock_product_locations",
    "stock_product_uoms",
    "stock_products",
    "stock_uom_categories",
    "stock_uoms",
    "stock_valuation_layers",
    "stock_warehouse_product_settings",
    "stock_warehouses",
    "stocktake_lines",
    "stocktakes",
    "supplier_bill_asset_lines",
    "supplier_bill_receipts",
    "supplier_bills",
    "supplier_credit_note_lines",
    "supplier_credit_notes",
    "supplier_payment_allocations",
    "supplier_payments",
    "suppliers",
    "workforce_migrations",
})


def _clear_company_data_postgresql(connection):
    """Reset approved operational tables atomically without touching system data.

    PostgreSQL TRUNCATE is transactional and restarts only the sequences owned
    by the selected operational tables. Before executing it, reject unknown
    tables and any foreign key from a preserved/unclassified table into a table
    that would be cleared. No CASCADE is used.
    """
    begin_write_transaction(connection)
    tables = sorted(table_names(connection))
    unknown_tables = sorted(set(tables) - PRESERVED_TABLES - DELETE_TABLES)
    if unknown_tables:
        raise RuntimeError(
            "توقف التصفير لحماية البيانات: توجد جداول غير مصنفة: "
            + ", ".join(unknown_tables)
            + ". راجع سياسة التصفير قبل المتابعة."
        )

    target_tables = sorted(set(tables) & DELETE_TABLES)
    target_set = set(target_tables)
    if not target_tables:
        connection.commit()
        return {
            "deleted_tables": 0,
            "deleted_rows": 0,
            "preserved_tables": sorted(PRESERVED_TABLES.intersection(tables)),
        }

    foreign_keys = connection.execute(
        """
        SELECT tc.table_name AS referencing_table,
               ccu.table_name AS referenced_table
          FROM information_schema.table_constraints tc
          JOIN information_schema.key_column_usage kcu
            ON kcu.constraint_catalog = tc.constraint_catalog
           AND kcu.constraint_schema = tc.constraint_schema
           AND kcu.constraint_name = tc.constraint_name
           AND kcu.table_name = tc.table_name
          JOIN information_schema.constraint_column_usage ccu
            ON ccu.constraint_catalog = tc.constraint_catalog
           AND ccu.constraint_schema = tc.constraint_schema
           AND ccu.constraint_name = tc.constraint_name
         WHERE tc.constraint_type = 'FOREIGN KEY'
           AND tc.table_schema = current_schema()
           AND ccu.table_schema = current_schema()
        """
    ).fetchall()
    unsafe_references = sorted({
        f"{row['referencing_table']} -> {row['referenced_table']}"
        for row in foreign_keys
        if row["referenced_table"] in target_set
        and row["referencing_table"] not in target_set
    })
    if unsafe_references:
        raise RuntimeError(
            "توقف التصفير: توجد جداول محفوظة أو غير مصنفة مرتبطة ببيانات التشغيل: "
            + ", ".join(unsafe_references)
            + ". لم يتم حذف أي بيانات."
        )

    deleted_rows = 0
    try:
        # Counts and TRUNCATE share one transaction. If any table or FK has
        # changed since classification, PostgreSQL rejects the operation and
        # the caller receives a full rollback.
        for table_name in target_tables:
            quoted = '"' + table_name.replace('"', '""') + '"'
            count = connection.execute(
                f"SELECT COUNT(*) FROM {quoted}"
            ).fetchone()[0]
            deleted_rows += int(count or 0)

        quoted_tables = ", ".join(
            '"' + name.replace('"', '""') + '"' for name in target_tables
        )
        connection.execute(
            f"TRUNCATE TABLE {quoted_tables} RESTART IDENTITY"
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise

    return {
        "deleted_tables": len(target_tables),
        "deleted_rows": deleted_rows,
        "preserved_tables": sorted(PRESERVED_TABLES.intersection(tables)),
    }


def clear_company_data(connection):
    """Delete only explicitly approved operational tables in one transaction.

    Unknown tables stop the operation before the first destructive statement.
    The caller must create a verified backup and authenticate the super-admin.
    """
    if database_dialect(connection) == "postgresql":
        try:
            return _clear_company_data_postgresql(connection)
        except Exception:
            connection.rollback()
            raise

    connection.commit()
    connection.execute("PRAGMA foreign_keys=OFF")
    deleted_tables = []
    deleted_rows = 0
    tables = []
    try:
        connection.execute("BEGIN IMMEDIATE")
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        unknown_tables = sorted(set(tables) - PRESERVED_TABLES - DELETE_TABLES)
        if unknown_tables:
            raise RuntimeError(
                "توقف التصفير لحماية البيانات: توجد جداول غير مصنفة: "
                + ", ".join(unknown_tables)
                + ". راجع سياسة التصفير قبل المتابعة."
            )

        for table_name in tables:
            if table_name not in DELETE_TABLES:
                continue
            quoted_name = '"' + table_name.replace('"', '""') + '"'
            count = connection.execute(f"SELECT COUNT(*) FROM {quoted_name}").fetchone()[0]
            connection.execute(f"DELETE FROM {quoted_name}")
            deleted_tables.append(table_name)
            deleted_rows += int(count or 0)

        violations = connection.execute("PRAGMA foreign_key_check").fetchmany(10)
        if violations:
            raise RuntimeError(
                "تعذر تصفير البيانات بسبب وجود علاقات مرجعية غير سليمة؛ لم يتم اعتماد أي حذف."
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.execute("PRAGMA foreign_keys=ON")

    return {
        "deleted_tables": len(deleted_tables),
        "deleted_rows": deleted_rows,
        "preserved_tables": sorted(PRESERVED_TABLES.intersection(tables)),
    }
