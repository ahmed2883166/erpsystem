"""Guarded company-data reset that preserves NEXUS system configuration and access."""

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


def clear_company_data(connection):
    """Delete only explicitly approved operational tables in one transaction.

    Unknown tables stop the operation before the first DELETE. The caller must
    create a verified backup and authenticate the super-admin first.
    """
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
