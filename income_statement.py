"""Income statement calculations shared by the UI and exports."""
from datetime import date
from decimal import Decimal

from accounting_core import register_decimal_aggregates


CLOSING_ENTRY_TYPES = ("year_end_closing", "year_end_closing_reversal")


def normalize_income_statement_period(date_from=None, date_to=None, today=None):
    """Return a valid inclusive period; blank/invalid input falls back to YTD."""
    today = today or date.today()
    default_from = today.replace(month=1, day=1)
    try:
        start = date.fromisoformat(str(date_from).strip()) if str(date_from or "").strip() else default_from
        end = date.fromisoformat(str(date_to).strip()) if str(date_to or "").strip() else today
        if start > end:
            raise ValueError("reversed period")
    except (TypeError, ValueError):
        start, end = default_from, today
    return start.isoformat(), end.isoformat()


def closing_entries_exclusion_sql(conn):
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(journal_entries)")}
    if "entry_type" in columns:
        return "COALESCE(je.entry_type,'general') NOT IN ('year_end_closing','year_end_closing_reversal')"
    tables = {row["name"] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='financial_year_journal_history'"
    )}
    if tables:
        return """je.id NOT IN (
            SELECT entry_id FROM financial_year_journal_history
            WHERE entry_kind='closing'
        ) AND je.id NOT IN (
            SELECT related_entry_id FROM financial_year_journal_history
            WHERE entry_kind='closing' AND action='reversed' AND related_entry_id IS NOT NULL
        )"""
    return "1=1"


def build_income_statement(conn, date_from, date_to):
    """Calculate revenue, COGS, gross profit, operating expenses and net profit."""
    register_decimal_aggregates(conn)
    closing_filter = closing_entries_exclusion_sql(conn)
    role_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='management_account_roles'"
    ).fetchone()
    direct_cost_ids = set()
    if role_table:
        direct_cost_ids = {
            row["account_id"] for row in conn.execute(
                "SELECT account_id FROM management_account_roles WHERE role='direct_cost'"
            )
        }

    conditions = [
        "je.status='posted'", "je.entry_date>=?", "je.entry_date<=?",
        closing_filter,
    ]
    rows = conn.execute(f"""SELECT a.id,a.code,a.name_ar,a.account_type,
        COALESCE(DECIMAL_SUM(CASE WHEN je.id IS NOT NULL THEN jl.debit ELSE '0' END),0) debit,
        COALESCE(DECIMAL_SUM(CASE WHEN je.id IS NOT NULL THEN jl.credit ELSE '0' END),0) credit
        FROM accounts a
        LEFT JOIN journal_lines jl ON jl.account_id=a.id
        LEFT JOIN journal_entries je ON je.id=jl.entry_id AND {' AND '.join(conditions)}
        WHERE a.account_type IN ('revenue','expense') AND a.is_group=0 AND a.active=1
        GROUP BY a.id ORDER BY a.account_type,a.code""",
        (date_from, date_to)).fetchall()

    revenue_rows, cogs_rows, operating_expense_rows = [], [], []
    revenue_total = Decimal("0")
    cogs_total = Decimal("0")
    operating_expense_total = Decimal("0")
    for row in rows:
        debit = Decimal(str(row["debit"] or "0"))
        credit = Decimal(str(row["credit"] or "0"))
        balance = credit - debit if row["account_type"] == "revenue" else debit - credit
        item = {**dict(row), "debit": debit, "credit": credit, "balance": balance}
        if row["account_type"] == "revenue":
            revenue_rows.append(item)
            revenue_total += balance
        elif row["id"] in direct_cost_ids:
            cogs_rows.append(item)
            cogs_total += balance
        else:
            operating_expense_rows.append(item)
            operating_expense_total += balance

    gross_profit = revenue_total - cogs_total
    net_profit = gross_profit - operating_expense_total
    return {
        "revenue_rows": revenue_rows,
        "cogs_rows": cogs_rows,
        "operating_expense_rows": operating_expense_rows,
        "revenue_total": revenue_total,
        "cogs_total": cogs_total,
        "gross_profit": gross_profit,
        "operating_expense_total": operating_expense_total,
        "net_profit": net_profit,
    }
