"""Decimal-safe management dashboard built from posted general-ledger entries."""
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP

CENT = Decimal("0.01")

# Account-code mappings come from the activity chart templates. Keep them here
# as activity mappings, not as one universal chart assumption.
ACTIVITY_ACCOUNTS = {
    "commercial": {
        "cash": ("1110", "1120"), "receivables": ("1130",), "payables": ("2110",),
        "inventory": ("1150",), "direct_cost": ("5100",),
    },
    "service": {
        "cash": ("1110",), "receivables": ("1120", "1130"), "payables": ("2100",),
        "inventory": (), "direct_cost": ("5100", "5200", "5300"),
    },
    "medical": {
        "cash": ("1110", "1120"), "receivables": ("1130", "1140"), "payables": ("2100",),
        "inventory": ("1150", "1160"), "direct_cost": ("5100", "5200", "5300"),
    },
    "restaurant": {
        "cash": ("1110", "1120"), "receivables": ("1130",), "payables": ("2100",),
        "inventory": ("1140", "1150", "1160"), "direct_cost": ("5100", "5200", "5300", "5400"),
    },
    "industrial": {
        "cash": ("1110",), "receivables": ("1120",), "payables": ("2100",),
        "inventory": ("1130", "1140", "1150", "1160"),
        "direct_cost": ("5100", "5200", "5300", "5400"),
    },
}


def _decimal(value):
    return Decimal(str(value or "0"))


def _rounded(value):
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _ratio(numerator, denominator):
    return _rounded(numerator / denominator) if denominator else None


def _percent(numerator, denominator):
    return _rounded(numerator * Decimal("100") / denominator) if denominator else None


def _change(current, previous):
    if not previous:
        return None
    return _rounded((current - previous) * Decimal("100") / abs(previous))


def account_balances(conn, date_from=None, date_to=None):
    """Return exact posted debit/credit totals by account for an optional period."""
    conditions = ["je.status='posted'"]
    params = []
    if date_from:
        conditions.append("je.entry_date>=?")
        params.append(date_from)
    if date_to:
        conditions.append("je.entry_date<=?")
        params.append(date_to)
    rows = conn.execute(f"""
        SELECT a.id,a.code,a.name_ar,a.account_type,a.is_group,jl.debit,jl.credit
        FROM journal_lines jl
        JOIN journal_entries je ON je.id=jl.entry_id
        JOIN accounts a ON a.id=jl.account_id
        WHERE {' AND '.join(conditions)}
    """, params).fetchall()
    balances = {}
    for row in rows:
        item = balances.setdefault(row["id"], {
            "code": row["code"], "name": row["name_ar"], "type": row["account_type"],
            "is_group": bool(row["is_group"]), "debit": Decimal("0"), "credit": Decimal("0"),
        })
        item["debit"] += _decimal(row["debit"])
        item["credit"] += _decimal(row["credit"])
    return balances


def income_figures(balances, direct_cost_codes):
    revenue = sum((b["credit"] - b["debit"] for b in balances.values()
                   if b["type"] == "revenue" and not b["is_group"]), Decimal("0"))
    expenses = sum((b["debit"] - b["credit"] for b in balances.values()
                    if b["type"] == "expense" and not b["is_group"]), Decimal("0"))
    direct_cost = sum((b["debit"] - b["credit"] for b in balances.values()
                       if b["code"] in direct_cost_codes and not b["is_group"]), Decimal("0"))
    return {
        "revenue": revenue, "expenses": expenses, "cogs": direct_cost,
        "net_profit": revenue - expenses,
    }


def _leaf_sum(balances, account_type, codes=None, prefix=None):
    total = Decimal("0")
    for item in balances.values():
        if item["is_group"] or item["type"] != account_type:
            continue
        if codes is not None and item["code"] not in codes:
            continue
        if prefix is not None and not item["code"].startswith(prefix):
            continue
        if account_type == "asset":
            total += item["debit"] - item["credit"]
        else:
            total += item["credit"] - item["debit"]
    return total


def build_management_snapshot(conn, start, end):
    """Build a dashboard snapshot for inclusive dates using posted journal entries."""
    if end < start:
        raise ValueError("تاريخ النهاية لازم يكون بعد تاريخ البداية.")
    settings = {}
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='company_settings'").fetchone():
        settings = {row["setting_key"]: row["setting_value"] for row in conn.execute(
            "SELECT setting_key,setting_value FROM company_settings WHERE setting_key IN ('activity','base_currency','company_name')"
        )}
    activity = settings.get("activity", "commercial")
    mapping = ACTIVITY_ACCOUNTS.get(activity, ACTIVITY_ACCOUNTS["commercial"])

    period_days = (end - start).days
    previous_end = start - timedelta(days=1)
    previous_start = previous_end - timedelta(days=period_days)
    current = income_figures(account_balances(conn, start.isoformat(), end.isoformat()), mapping["direct_cost"])
    previous = income_figures(account_balances(conn, previous_start.isoformat(), previous_end.isoformat()), mapping["direct_cost"])
    as_of = account_balances(conn, date_to=end.isoformat())
    cumulative = income_figures(as_of, mapping["direct_cost"])

    assets = _leaf_sum(as_of, "asset")
    liabilities = _leaf_sum(as_of, "liability")
    equity = _leaf_sum(as_of, "equity")
    # Works both before and after period-close entries: closed profit is in equity,
    # while unclosed profit remains in revenue/expense account balances.
    balance_ok = assets == liabilities + equity + cumulative["net_profit"]

    current_assets = _leaf_sum(as_of, "asset", prefix="11")
    current_liabilities = _leaf_sum(as_of, "liability", prefix="21")
    gross_profit = current["revenue"] - current["cogs"]
    return {
        "activity": activity,
        "company_name": settings.get("company_name", "NEXUS ERP"),
        "base_currency": settings.get("base_currency", "EGP"),
        "start": start, "end": end, "prev_start": previous_start, "prev_end": previous_end,
        "current": current, "previous": previous,
        "changes": {
            "revenue": _change(current["revenue"], previous["revenue"]),
            "expenses": _change(current["expenses"], previous["expenses"]),
            "net_profit": _change(current["net_profit"], previous["net_profit"]),
        },
        "gross_margin": _percent(gross_profit, current["revenue"]),
        "cash": _leaf_sum(as_of, "asset", codes=mapping["cash"]),
        "receivables": _leaf_sum(as_of, "asset", codes=mapping["receivables"]),
        "payables": _leaf_sum(as_of, "liability", codes=mapping["payables"]),
        "inventory": _leaf_sum(as_of, "asset", codes=mapping["inventory"]),
        "working_capital": current_assets - current_liabilities,
        "current_ratio": _ratio(current_assets, current_liabilities),
        "quick_ratio": _ratio(current_assets - _leaf_sum(as_of, "asset", codes=mapping["inventory"]), current_liabilities),
        "balance_ok": balance_ok,
        "total_assets": assets,
        "total_liabilities": liabilities,
        "total_equity": equity,
    }
