"""Guarded company-data reset that preserves NEXUS system configuration and access."""

# These tables define the installation, accounting structure, or system policy.
# Operational/master records (customers, suppliers, products, warehouses, documents,
# movements, journals, payroll runs, etc.) are deliberately cleared.
PRESERVED_TABLES = frozenset({
    "users",
    "accounts",
    "company_settings",
    "currencies",
    "tax_rates",
    "audit_log",
    "activity_profiles",
    "cash_flow_account_mappings",
    "management_account_roles",
    "payroll_rule_sets",
    "payroll_tax_brackets",
    "payroll_tax_tier_brackets",
    "fx_exchange_rates",
})


def clear_company_data(connection):
    """Delete company operational/master data atomically, keeping system setup intact.

    The caller must create a verified backup and authenticate the super-admin first.
    Returns a small summary only; no business data is copied into the audit log.
    """
    connection.commit()
    connection.execute("PRAGMA foreign_keys=OFF")
    deleted_tables = []
    deleted_rows = 0
    try:
        connection.execute("BEGIN IMMEDIATE")
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        for table_name in tables:
            if table_name in PRESERVED_TABLES:
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
