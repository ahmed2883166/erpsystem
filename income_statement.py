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
    account_columns = {row["name"] for row in conn.execute("PRAGMA table_info(accounts)")}
    section_expr = "a.statement_section" if "statement_section" in account_columns else "NULL"
    subtype_expr = "a.account_subtype" if "account_subtype" in account_columns else "NULL"
    rows = conn.execute(f"""SELECT a.id,a.code,a.name_ar,a.account_type,
        {section_expr} statement_section,{subtype_expr} account_subtype,
        COALESCE(DECIMAL_SUM(CASE WHEN je.id IS NOT NULL THEN jl.debit ELSE '0' END),0) debit,
        COALESCE(DECIMAL_SUM(CASE WHEN je.id IS NOT NULL THEN jl.credit ELSE '0' END),0) credit
        FROM accounts a
        LEFT JOIN journal_lines jl ON jl.account_id=a.id
        LEFT JOIN journal_entries je ON je.id=jl.entry_id AND {' AND '.join(conditions)}
        WHERE a.account_type IN ('revenue','expense') AND a.is_group=0 AND a.active=1
        GROUP BY a.id ORDER BY a.account_type,a.code""",
        (date_from, date_to, *dimension_params)).fetchall()

    # Disposal gains and losses must be presented by their economic nature,
    # not by the configured account's type. Split the disposal journal lines
    # from the account's ordinary balance so a shared gain/loss account (or
    # unrelated postings to that account) cannot turn gains into expenses.
    disposal_loss_account_ids = set()
    disposal_line_adjustments = {}
    disposal_result_rows = []
    disposal_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='asset_disposals'"
    ).fetchone()
    if disposal_table:
        disposal_columns = {row["name"] for row in conn.execute("PRAGMA table_info(asset_disposals)")}
        if {"gain_loss_account_id", "gain_loss_amount", "disposal_date"}.issubset(disposal_columns):
            if "journal_entry_id" in disposal_columns:
                disposal_records = conn.execute(
                    f"""SELECT ad.id,ad.gain_loss_account_id,ad.gain_loss_amount,
                               a.code,a.name_ar,a.account_type,
                               jl.debit,jl.credit
                        FROM asset_disposals ad
                        JOIN journal_entries je ON je.id=ad.journal_entry_id
                        JOIN journal_lines jl ON jl.entry_id=je.id
                             AND jl.account_id=ad.gain_loss_account_id
                        JOIN accounts a ON a.id=ad.gain_loss_account_id
                        WHERE {' AND '.join(conditions)}
                          AND ad.disposal_date>=? AND ad.disposal_date<=?
                          AND CAST(ad.gain_loss_amount AS NUMERIC)<>0""",
                    (date_from, date_to, *dimension_params, date_from, date_to),
                ).fetchall()
                for disposal in disposal_records:
                    amount = Decimal(str(disposal["gain_loss_amount"] or "0"))
                    debit = Decimal(str(disposal["debit"] or "0"))
                    credit = Decimal(str(disposal["credit"] or "0"))
                    account_id = disposal["gain_loss_account_id"]
                    previous = disposal_line_adjustments.setdefault(
                        account_id, [Decimal("0"), Decimal("0")]
                    )
                    previous[0] += debit
                    previous[1] += credit
                    effective_type = "revenue" if amount > 0 else "expense"
                    value = abs(amount)
                    disposal_result_rows.append({
                        "id": "asset-disposal-" + str(disposal["id"]),
                        "code": disposal["code"] + "-D" + str(disposal["id"]),
                        "name_ar": (
                            "ربح استبعاد أصل - " if amount > 0 else "خسارة استبعاد أصل - "
                        ) + disposal["name_ar"],
                        "account_type": effective_type,
                        "statement_section": "other_income" if amount > 0 else "operating_expenses",
                        "account_subtype": "asset_disposal",
                        "debit": value if amount < 0 else Decimal("0"),
                        "credit": value if amount > 0 else Decimal("0"),
                        "balance": value,
                        "force_operating_expense": amount < 0,
                    })
            else:
                # Compatibility with older disposal-register schemas that did
                # not retain the linked journal entry id.
                disposal_loss_account_ids = {
                    row["gain_loss_account_id"] for row in conn.execute(
                        """SELECT DISTINCT gain_loss_account_id FROM asset_disposals
                           WHERE disposal_date>=? AND disposal_date<=?
                             AND CAST(gain_loss_amount AS NUMERIC)<0""",
                        (date_from, date_to),
                    ).fetchall()
                }

    revenue_rows, cogs_rows, operating_expense_rows = [], [], []
    revenue_total = Decimal("0")
    cogs_total = Decimal("0")
    operating_expense_total = Decimal("0")
    for row in rows:
        debit = Decimal(str(row["debit"] or "0"))
        credit = Decimal(str(row["credit"] or "0"))
        adjustment = disposal_line_adjustments.get(row["id"])
        if adjustment:
            debit -= adjustment[0]
            credit -= adjustment[1]
        effective_type = (
            "expense" if row["account_type"] == "revenue"
            and row["id"] in disposal_loss_account_ids else row["account_type"]
        )
        balance = credit - debit if effective_type == "revenue" else debit - credit
        item = {**dict(row), "account_type": effective_type,
                "debit": debit, "credit": credit, "balance": balance}
        if effective_type == "revenue":
            revenue_rows.append(item)
            revenue_total += balance
        elif row["id"] in direct_cost_ids or row["statement_section"] == "direct_costs":
            # Existing explicit role mappings remain valid; chart metadata is the
            # fallback for newly added activity-specific direct-cost accounts.
            cogs_rows.append(item)
            cogs_total += balance
        else:
            operating_expense_rows.append(item)
            operating_expense_total += balance

    for item in disposal_result_rows:
        if item["account_type"] == "revenue":
            revenue_rows.append(item)
            revenue_total += item["balance"]
        else:
            operating_expense_rows.append(item)
            operating_expense_total += item["balance"]

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
