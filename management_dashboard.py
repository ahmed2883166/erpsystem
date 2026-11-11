"""Decimal-safe management dashboard built from posted general-ledger entries."""
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP
from accounting_core import register_decimal_aggregates

CENT = Decimal("0.01")

# Account-code mappings come from the activity chart templates. Keep them here
# as activity mappings, not as one universal chart assumption.
ACTIVITY_ACCOUNTS = {
    "commercial": {
        "cash": ("1110", "1120"), "receivables": ("1130",), "payables": ("2110",),
        "inventory": ("1150",), "quick_assets": ("1110", "1120", "1130", "1140"), "direct_cost": ("5100",),
    },
    "service": {
        "cash": ("1110",), "receivables": ("1120", "1130"), "payables": ("2100",),
        "inventory": (), "quick_assets": ("1110", "1120", "1130"), "direct_cost": ("5100", "5200", "5300"),
    },
    "medical": {
        "cash": ("1110", "1120"), "receivables": ("1130", "1140"), "payables": ("2100",),
        "inventory": ("1150", "1160"), "quick_assets": ("1110", "1120", "1130", "1140"), "direct_cost": ("5100", "5200", "5300"),
    },
    "restaurant": {
        "cash": ("1110", "1120"), "receivables": ("1130",), "payables": ("2100",),
        "inventory": ("1140", "1150", "1160"), "quick_assets": ("1110", "1120", "1130"), "direct_cost": ("5100", "5200", "5300", "5400"),
    },
    "industrial": {
        "cash": ("1110",), "receivables": ("1120",), "payables": ("2100",),
        "inventory": ("1130", "1140", "1150", "1160"),
        "quick_assets": ("1110", "1120"), "direct_cost": ("5400",),
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
    # Aggregate monetary TEXT values inside SQLite with exact Decimal aggregates.
    # This avoids transferring every journal line to Python as the ledger grows.
    register_decimal_aggregates(conn)
    rows = conn.execute(f"""
        SELECT a.id,a.code,a.name_ar,a.account_type,a.is_group,
               COALESCE(DECIMAL_SUM(jl.debit),0) debit,
               COALESCE(DECIMAL_SUM(jl.credit),0) credit
        FROM journal_lines jl
        JOIN journal_entries je ON je.id=jl.entry_id
        JOIN accounts a ON a.id=jl.account_id
        WHERE {' AND '.join(conditions)}
        GROUP BY a.id,a.code,a.name_ar,a.account_type,a.is_group
    """, params).fetchall()
    return {
        row["id"]: {
            "id": row["id"], "code": row["code"], "name": row["name_ar"], "type": row["account_type"],
            "is_group": bool(row["is_group"]), "debit": _decimal(row["debit"]),
            "credit": _decimal(row["credit"]),
        }
        for row in rows
    }


def income_figures(balances, direct_cost_codes=None, direct_cost_ids=None):
    revenue = sum((b["credit"] - b["debit"] for b in balances.values()
                   if b["type"] == "revenue" and not b["is_group"]), Decimal("0"))
    expenses = sum((b["debit"] - b["credit"] for b in balances.values()
                    if b["type"] == "expense" and not b["is_group"]), Decimal("0"))
    direct_cost = sum((b["debit"] - b["credit"] for b in balances.values()
                       if ((direct_cost_ids is not None and b["id"] in direct_cost_ids) or (direct_cost_ids is None and direct_cost_codes is not None and b["code"] in direct_cost_codes)) and not b["is_group"]), Decimal("0"))
    return {
        "revenue": revenue, "expenses": expenses, "cogs": direct_cost,
        "net_profit": revenue - expenses,
    }


def _leaf_sum(balances, account_type, codes=None, prefix=None, account_ids=None):
    total = Decimal("0")
    for item in balances.values():
        if item["is_group"] or item["type"] != account_type:
            continue
        if codes is not None and item["code"] not in codes:
            continue
        if prefix is not None and not item["code"].startswith(prefix):
            continue
        if account_ids is not None and item["id"] not in account_ids:
            continue
        if account_type == "asset":
            total += item["debit"] - item["credit"]
        else:
            total += item["credit"] - item["debit"]
    return total


MANAGEMENT_ROLES = {
    "cash": ("النقدية والبنوك", {"asset"}),
    "receivables": ("العملاء والمدينون", {"asset"}),
    "payables": ("الموردون والدائنون", {"liability"}),
    "inventory": ("المخزون", {"asset"}),
    "quick_assets": ("الأصول السريعة", {"asset"}),
    "current_assets": ("الأصول المتداولة", {"asset"}),
    "current_liabilities": ("الالتزامات المتداولة", {"liability"}),
    "direct_cost": ("التكاليف المباشرة", {"expense"}),
}


def load_management_account_roles(conn, activity):
    """Load user-configurable account roles; seed activity defaults only once."""
    conn.execute("CREATE INDEX IF NOT EXISTS idx_journal_entries_status_date ON journal_entries(status,entry_date,id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_journal_lines_entry_account ON journal_lines(entry_id,account_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_journal_lines_account_entry ON journal_lines(account_id,entry_id)")
    conn.execute("""CREATE TABLE IF NOT EXISTS management_account_roles(
        account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
        role TEXT NOT NULL, PRIMARY KEY(account_id,role))""")
    configured = conn.execute("SELECT COUNT(*) FROM management_account_roles").fetchone()[0]
    if not configured:
        defaults = ACTIVITY_ACCOUNTS.get(activity, ACTIVITY_ACCOUNTS["commercial"])
        for role in ("cash", "receivables", "payables", "inventory", "quick_assets", "direct_cost"):
            for code in defaults.get(role, ()):
                conn.execute("""INSERT OR IGNORE INTO management_account_roles(account_id,role)
                    SELECT id,? FROM accounts WHERE code=? AND is_group=0""", (role, code))
        conn.execute("""INSERT OR IGNORE INTO management_account_roles(account_id,role)
            SELECT id,'current_assets' FROM accounts WHERE is_group=0 AND account_type='asset' AND code LIKE '11%'""")
        conn.execute("""INSERT OR IGNORE INTO management_account_roles(account_id,role)
            SELECT id,'current_liabilities' FROM accounts WHERE is_group=0 AND account_type='liability' AND code LIKE '21%'""")
        conn.commit()
    result = {role: set() for role in MANAGEMENT_ROLES}
    for row in conn.execute("SELECT account_id,role FROM management_account_roles"):
        if row["role"] in result:
            result[row["role"]].add(row["account_id"])
    return result


def _top_accounts(balances, account_type, value_key, limit=8):
    rows = []
    for account_id, item in balances.items():
        if item["is_group"] or item["type"] != account_type:
            continue
        value = item["credit"] - item["debit"] if value_key == "credit" else item["debit"] - item["credit"]
        if value:
            rows.append({"id": account_id, "code": item["code"], "name": item["name"], "value": value})
    rows.sort(key=lambda x: abs(x["value"]), reverse=True)
    rows = rows[:limit]
    maximum = max((abs(x["value"]) for x in rows), default=Decimal("0"))
    for row in rows:
        row["width"] = float((abs(row["value"]) * Decimal("100") / maximum).quantize(Decimal("0.1"))) if maximum else 0
    return rows


def build_management_snapshot(conn, start, end):
    """Build a Decimal-safe management snapshot for inclusive dates."""
    if end < start:
        raise ValueError("تاريخ النهاية لازم يكون بعد تاريخ البداية.")
    settings = {}
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='company_settings'").fetchone():
        settings = {row["setting_key"]: row["setting_value"] for row in conn.execute(
            "SELECT setting_key,setting_value FROM company_settings WHERE setting_key IN ('activity','base_currency','company_name')"
        )}
    activity = settings.get("activity", "commercial")
    roles = load_management_account_roles(conn, activity)
    period_days = (end - start).days
    previous_end = start - timedelta(days=1)
    previous_start = previous_end - timedelta(days=period_days)
    current_balances = account_balances(conn, start.isoformat(), end.isoformat())
    previous_balances = account_balances(conn, previous_start.isoformat(), previous_end.isoformat())
    as_of = account_balances(conn, date_to=end.isoformat())
    current = income_figures(current_balances, direct_cost_ids=roles["direct_cost"])
    previous = income_figures(previous_balances, direct_cost_ids=roles["direct_cost"])
    cumulative = income_figures(as_of, direct_cost_ids=roles["direct_cost"])
    assets = _leaf_sum(as_of, "asset")
    liabilities = _leaf_sum(as_of, "liability")
    equity = _leaf_sum(as_of, "equity")
    balance_ok = assets == liabilities + equity + cumulative["net_profit"]
    current_assets = _leaf_sum(as_of, "asset", account_ids=roles["current_assets"])
    current_liabilities = _leaf_sum(as_of, "liability", account_ids=roles["current_liabilities"])
    gross_profit = current["revenue"] - current["cogs"]
    details = []
    for role in ("cash", "receivables", "payables", "inventory"):
        for account_id in sorted(roles[role]):
            item = as_of.get(account_id)
            if not item:
                continue
            value = item["debit"] - item["credit"] if item["type"] == "asset" else item["credit"] - item["debit"]
            details.append({"role": role, "id": account_id, "code": item["code"], "name": item["name"], "balance": value})
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
        "cash": _leaf_sum(as_of, "asset", account_ids=roles["cash"]),
        "receivables": _leaf_sum(as_of, "asset", account_ids=roles["receivables"]),
        "payables": _leaf_sum(as_of, "liability", account_ids=roles["payables"]),
        "inventory": _leaf_sum(as_of, "asset", account_ids=roles["inventory"]),
        "working_capital": current_assets - current_liabilities,
        "current_ratio": _ratio(current_assets, current_liabilities),
        "quick_ratio": _ratio(_leaf_sum(as_of, "asset", account_ids=roles["quick_assets"]), current_liabilities),
        "balance_ok": balance_ok,
        "total_assets": assets,
        "total_liabilities": liabilities,
        "total_equity": equity,
        "revenue_accounts": _top_accounts(current_balances, "revenue", "credit"),
        "expense_accounts": _top_accounts(current_balances, "expense", "debit"),
        "account_details": details,
        "account_roles": roles,
    }
