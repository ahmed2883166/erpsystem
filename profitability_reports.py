"""Management profitability reports based on posted accounting and sales records.

Branch and cost-center results use posted general-ledger dimensions. Warehouse
results use posted sales invoices and their stored line-level valuation costs;
they are gross contribution figures because operating overhead is not assigned
to warehouses in the current data model.
"""
from decimal import Decimal

from accounting_core import register_decimal_aggregates
from income_statement import closing_entries_exclusion_sql


def _table_exists(conn, name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _columns(conn, table):
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _amount(value):
    return Decimal(str(value or "0"))


def _grouped_gl_profitability(conn, date_from, date_to, dimension):
    table, column, label = {
        "branch_id": ("branches", "branch_id", "الفرع"),
        "cost_center_id": ("cost_centers", "cost_center_id", "مركز التكلفة / النشاط"),
    }[dimension]
    journal_columns = _columns(conn, "journal_lines")
    if column not in journal_columns or not _table_exists(conn, table):
        return [], False

    account_columns = _columns(conn, "accounts")
    section_expr = "a.statement_section" if "statement_section" in account_columns else "NULL"
    closing_filter = closing_entries_exclusion_sql(conn)
    register_decimal_aggregates(conn)
    rows = conn.execute(
        f"""SELECT d.id,d.code,d.name_ar,
             COALESCE(DECIMAL_SUM(CASE WHEN a.account_type='revenue'
               THEN jl.credit ELSE '0' END),'0') revenue_credit,
             COALESCE(DECIMAL_SUM(CASE WHEN a.account_type='revenue'
               THEN jl.debit ELSE '0' END),'0') revenue_debit,
             COALESCE(DECIMAL_SUM(CASE WHEN a.account_type='expense'
               AND (EXISTS(SELECT 1 FROM management_account_roles mar
                           WHERE mar.account_id=a.id AND mar.role='direct_cost')
                    OR {section_expr}='direct_costs')
               THEN jl.debit ELSE '0' END),'0') cogs_debit,
             COALESCE(DECIMAL_SUM(CASE WHEN a.account_type='expense'
               AND (EXISTS(SELECT 1 FROM management_account_roles mar
                           WHERE mar.account_id=a.id AND mar.role='direct_cost')
                    OR {section_expr}='direct_costs')
               THEN jl.credit ELSE '0' END),'0') cogs_credit,
             COALESCE(DECIMAL_SUM(CASE WHEN a.account_type='expense'
               AND NOT (EXISTS(SELECT 1 FROM management_account_roles mar
                               WHERE mar.account_id=a.id AND mar.role='direct_cost')
                        OR {section_expr}='direct_costs')
               THEN jl.debit ELSE '0' END),'0') expense_debit,
             COALESCE(DECIMAL_SUM(CASE WHEN a.account_type='expense'
               AND NOT (EXISTS(SELECT 1 FROM management_account_roles mar
                               WHERE mar.account_id=a.id AND mar.role='direct_cost')
                        OR {section_expr}='direct_costs')
               THEN jl.credit ELSE '0' END),'0') expense_credit,
             COUNT(DISTINCT je.id) journal_count
           FROM journal_lines jl
           JOIN journal_entries je ON je.id=jl.entry_id
           JOIN accounts a ON a.id=jl.account_id
           LEFT JOIN {table} d ON d.id=jl.{column}
           WHERE je.status='posted' AND je.entry_date>=? AND je.entry_date<=?
             AND {closing_filter}
             AND a.account_type IN ('revenue','expense') AND a.is_group=0
           GROUP BY d.id,d.code,d.name_ar
           ORDER BY CASE WHEN d.id IS NULL THEN 1 ELSE 0 END,d.code,d.id""",
        (date_from, date_to),
    ).fetchall()

    result = []
    for row in rows:
        revenue = _amount(row["revenue_credit"]) - _amount(row["revenue_debit"])
        cogs = _amount(row["cogs_debit"]) - _amount(row["cogs_credit"])
        expenses = _amount(row["expense_debit"]) - _amount(row["expense_credit"])
        result.append({
            "id": row["id"], "code": row["code"],
            "name": row["name_ar"] or "غير موزع",
            "label": label, "revenue": revenue, "cogs": cogs,
            "gross_profit": revenue - cogs,
            "operating_expenses": expenses,
            "net_profit": revenue - cogs - expenses,
            "journal_count": int(row["journal_count"] or 0),
            "unassigned": row["id"] is None,
            "basis": "القيود المرحلة ذات البُعد التحليلي",
        })
    return result, True


def _warehouse_profitability(conn, date_from, date_to):
    required = ("sales_invoices", "sales_invoice_lines", "sales_credit_notes",
                "sales_credit_note_lines", "stock_warehouses")
    if not all(_table_exists(conn, table) for table in required):
        return [], False

    register_decimal_aggregates(conn)
    invoice_rows = conn.execute(
        """SELECT i.warehouse_id,
                  COALESCE(DECIMAL_SUM(i.subtotal),'0') revenue,
                  COALESCE(DECIMAL_SUM(COALESCE(lines.cost_total,'0')),'0') cogs
           FROM sales_invoices i
           LEFT JOIN (
             SELECT invoice_id,DECIMAL_SUM(cost_total) cost_total
             FROM sales_invoice_lines GROUP BY invoice_id
           ) lines ON lines.invoice_id=i.id
           WHERE i.status='posted' AND i.invoice_date>=? AND i.invoice_date<=?
           GROUP BY i.warehouse_id""",
        (date_from, date_to),
    ).fetchall()
    credit_rows = conn.execute(
        """SELECT i.warehouse_id,
                  COALESCE(DECIMAL_SUM(cn.subtotal),'0') revenue,
                  COALESCE(DECIMAL_SUM(COALESCE(lines.cost_total,'0')),'0') cogs
           FROM sales_credit_notes cn
           JOIN sales_invoices i ON i.id=cn.invoice_id
           LEFT JOIN (
             SELECT credit_note_id,DECIMAL_SUM(cost_total) cost_total
             FROM sales_credit_note_lines GROUP BY credit_note_id
           ) lines ON lines.credit_note_id=cn.id
           WHERE cn.status='posted' AND cn.credit_date>=? AND cn.credit_date<=?
           GROUP BY i.warehouse_id""",
        (date_from, date_to),
    ).fetchall()

    totals = {}
    for row in invoice_rows:
        totals[row["warehouse_id"]] = [
            _amount(row["revenue"]), _amount(row["cogs"])
        ]
    for row in credit_rows:
        bucket = totals.setdefault(row["warehouse_id"], [Decimal("0"), Decimal("0")])
        bucket[0] -= _amount(row["revenue"])
        bucket[1] -= _amount(row["cogs"])

    warehouse_rows = conn.execute(
        "SELECT id,code,name_ar FROM stock_warehouses ORDER BY code,id"
    ).fetchall()
    names = {row["id"]: dict(row) for row in warehouse_rows}
    result = []
    for warehouse_id, (revenue, cogs) in totals.items():
        warehouse = names.get(warehouse_id)
        result.append({
            "id": warehouse_id,
            "code": warehouse["code"] if warehouse else "",
            "name": warehouse["name_ar"] if warehouse else "فواتير دون مخزن محدد",
            "revenue": revenue, "cogs": cogs,
            "gross_profit": revenue - cogs,
            "operating_expenses": None, "net_profit": None,
            "journal_count": None, "unassigned": warehouse_id is None,
            "basis": "صافي فواتير البيع وإشعاراتها الدائنة وتكلفة البنود المحفوظة",
        })
    result.sort(key=lambda row: (row["unassigned"], row["code"], row["name"]))
    return result, True


def build_profitability_report(conn, date_from, date_to):
    """Return profitability by branch, cost center/activity, and warehouse."""
    branches, branch_available = _grouped_gl_profitability(
        conn, date_from, date_to, "branch_id"
    )
    cost_centers, cost_center_available = _grouped_gl_profitability(
        conn, date_from, date_to, "cost_center_id"
    )
    warehouses, warehouse_available = _warehouse_profitability(
        conn, date_from, date_to
    )

    activity = None
    if _table_exists(conn, "activity_profiles"):
        setting = conn.execute(
            "SELECT setting_value FROM company_settings WHERE setting_key='activity_profile_code'"
        ).fetchone() if _table_exists(conn, "company_settings") else None
        if not setting and _table_exists(conn, "company_settings"):
            setting = conn.execute(
                "SELECT setting_value FROM company_settings WHERE setting_key='activity'"
            ).fetchone()
        if setting:
            row = conn.execute(
                "SELECT code,name,base_activity FROM activity_profiles WHERE code=?",
                (setting["setting_value"],),
            ).fetchone()
            if row:
                activity = dict(row)
    return {
        "branches": branches, "cost_centers": cost_centers, "warehouses": warehouses,
        "availability": {
            "branches": branch_available,
            "cost_centers": cost_center_available,
            "warehouses": warehouse_available,
        },
        "active_activity": activity,
        "date_from": date_from, "date_to": date_to,
    }
