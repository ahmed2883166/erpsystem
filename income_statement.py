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



def comparison_period(date_from, date_to, mode="previous_year"):
    """Return an inclusive comparison range: prior period or same dates last year."""
    start = date.fromisoformat(date_from)
    end = date.fromisoformat(date_to)
    if mode == "previous_period":
        previous_end = start - date.resolution
        previous_start = previous_end - (end - start)
        return previous_start.isoformat(), previous_end.isoformat()
    if mode == "previous_year":
        def shift_year(value):
            year = value.year - 1
            next_month = value.month % 12 + 1
            next_year = year + (1 if value.month == 12 else 0)
            last_day = (date(next_year, next_month, 1) - date.resolution).day
            return date(year, value.month, min(value.day, last_day))
        return shift_year(start).isoformat(), shift_year(end).isoformat()
    return None


def add_income_statement_comparison(current, previous):
    """Merge current and prior account rows and calculate variances."""
    for section in ("revenue_rows", "cogs_rows", "operating_expense_rows"):
        current_by_id = {row["id"]: row for row in current[section]}
        previous_by_id = {row["id"]: row for row in previous[section]}
        merged = []
        for account_id in sorted(
            set(current_by_id) | set(previous_by_id),
            key=lambda key: (current_by_id.get(key) or previous_by_id[key])["code"],
        ):
            row = dict(current_by_id.get(account_id) or previous_by_id[account_id])
            if account_id not in current_by_id:
                row.update(debit=Decimal("0"), credit=Decimal("0"), balance=Decimal("0"))
            prior_balance = previous_by_id.get(account_id, {}).get("balance", Decimal("0"))
            variance = row["balance"] - prior_balance
            row.update(
                previous_balance=prior_balance,
                variance=variance,
                variance_percent=(variance / abs(prior_balance) * Decimal("100"))
                if prior_balance else None,
            )
            merged.append(row)
        current[section] = merged

    comparison = {}
    for key in (
        "revenue_total", "cogs_total", "gross_profit",
        "operating_expense_total", "net_profit",
    ):
        prior = previous[key]
        variance = current[key] - prior
        comparison[key] = prior
        comparison[key + "_variance"] = variance
        comparison[key + "_variance_percent"] = (
            variance / abs(prior) * Decimal("100")
        ) if prior else None
    current["comparison"] = comparison
    return current


def build_income_statement(conn, date_from, date_to, dimensions=None):
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
    dimension_params = []
    for key in ("branch_id", "cost_center_id", "project_id", "customer_id", "salesperson_id"):
        value = (dimensions or {}).get(key)
        if value is not None:
            conditions.append(f"jl.{key}=?")
            dimension_params.append(value)
    rows = conn.execute(f"""SELECT a.id,a.code,a.name_ar,a.account_type,
        COALESCE(DECIMAL_SUM(CASE WHEN je.id IS NOT NULL THEN jl.debit ELSE '0' END),0) debit,
        COALESCE(DECIMAL_SUM(CASE WHEN je.id IS NOT NULL THEN jl.credit ELSE '0' END),0) credit
        FROM accounts a
        LEFT JOIN journal_lines jl ON jl.account_id=a.id
        LEFT JOIN journal_entries je ON je.id=jl.entry_id AND {' AND '.join(conditions)}
        WHERE a.account_type IN ('revenue','expense') AND a.is_group=0 AND a.active=1
        GROUP BY a.id ORDER BY a.account_type,a.code""",
        (date_from, date_to, *dimension_params)).fetchall()

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
