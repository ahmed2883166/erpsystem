"""Decimal-safe management dashboard built from posted general-ledger entries."""
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from collections import OrderedDict
from copy import deepcopy
from threading import RLock
from accounting_core import register_decimal_aggregates

CENT = Decimal("0.01")

# Small per-process cache. Database triggers increment the version whenever a
# journal or dashboard mapping/settings row changes, so cached values cannot
# survive a financial mutation. A bounded cache prevents unbounded growth.
_SNAPSHOT_CACHE = OrderedDict()
_SNAPSHOT_CACHE_LOCK = RLock()
_SNAPSHOT_CACHE_LIMIT = 64

# Account roles are defined beside the activity-specific chart templates.
from chart_templates import ACTIVITY_ACCOUNT_ROLES as ACTIVITY_ACCOUNTS


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


def account_balances(conn, date_from=None, date_to=None, dimensions=None, exclude_closing_entries=False):
    """Return exact posted debit/credit totals; optionally omit year-end closing journals."""
    conditions = ["je.status='posted'"]
    params = []
    if exclude_closing_entries:
        entry_columns = {row["name"] for row in conn.execute("PRAGMA table_info(journal_entries)")}
        if "entry_type" in entry_columns:
            conditions.append("COALESCE(je.entry_type,'general') NOT IN ('year_end_closing','year_end_closing_reversal')")
        else:
            history_exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='financial_year_journal_history'"
            ).fetchone()
            if history_exists:
                conditions.append("""je.id NOT IN (
                    SELECT entry_id FROM financial_year_journal_history WHERE entry_kind='closing'
                ) AND je.id NOT IN (
                    SELECT related_entry_id FROM financial_year_journal_history
                    WHERE entry_kind='closing' AND action='reversed' AND related_entry_id IS NOT NULL
                )""")
    if date_from:
        conditions.append("je.entry_date>=?")
        params.append(date_from)
    if date_to:
        conditions.append("je.entry_date<=?")
        params.append(date_to)
    dimensions = dimensions or {}
    available_columns = {row["name"] for row in conn.execute("PRAGMA table_info(journal_lines)")}
    for key in ("branch_id", "cost_center_id", "project_id", "customer_id", "salesperson_id"):
        value = dimensions.get(key)
        if value not in (None, ""):
            if key not in available_columns:
                raise ValueError("مخطط الأبعاد التحليلية غير محدث. أعد تشغيل البرنامج لإكمال التحديث.")
            conditions.append(f"jl.{key}=?")
            params.append(int(value))
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


def initialize_management_dashboard_schema(conn, activity):
    """Create dashboard support schema and seed defaults once during app initialization."""
    conn.execute("CREATE INDEX IF NOT EXISTS idx_journal_entries_status_date ON journal_entries(status,entry_date,id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_journal_lines_entry_account ON journal_lines(entry_id,account_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_journal_lines_account_entry ON journal_lines(account_id,entry_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_journal_entries_entry_date ON journal_entries(entry_date,id)")
    conn.execute("""CREATE TABLE IF NOT EXISTS management_dashboard_data_version(
        id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL DEFAULT 0)""")
    conn.execute("INSERT OR IGNORE INTO management_dashboard_data_version(id,version) VALUES(1,0)")
    for table, event in (
        ("journal_entries","INSERT"),("journal_entries","UPDATE"),("journal_entries","DELETE"),
        ("journal_lines","INSERT"),("journal_lines","UPDATE"),("journal_lines","DELETE"),
        ("accounts","INSERT"),("accounts","UPDATE"),("accounts","DELETE"),
    ):
        trigger = "trg_dashboard_version_" + table + "_" + event.lower()
        conn.execute(f"""CREATE TRIGGER IF NOT EXISTS {trigger} AFTER {event} ON {table}
            BEGIN UPDATE management_dashboard_data_version SET version=version+1 WHERE id=1; END""")
    conn.execute("""CREATE TABLE IF NOT EXISTS management_account_roles(
        account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
        role TEXT NOT NULL, PRIMARY KEY(account_id,role))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS management_dashboard_meta(
        meta_key TEXT PRIMARY KEY, meta_value TEXT NOT NULL)""")
    meta = {row["meta_key"]: row["meta_value"] for row in conn.execute("SELECT meta_key,meta_value FROM management_dashboard_meta")}
    customized = meta.get("roles_customized") == "1"
    previous_activity = meta.get("seed_activity")
    needs_seed = meta.get("roles_initialized") != "1"
    activity_changed = previous_activity != activity
    # Only an explicit customization marker protects role assignments.
    # Legacy auto-seeded rows without metadata must be rebuilt for the current
    # activity; otherwise a commercial mapping can misclassify industrial 1120/1130.
    if (needs_seed or activity_changed) and not customized:
        conn.execute("DELETE FROM management_account_roles")
        defaults = ACTIVITY_ACCOUNTS.get(activity, ACTIVITY_ACCOUNTS["commercial"])
        for role, codes in defaults.items():
            for code in codes:
                conn.execute("""INSERT OR IGNORE INTO management_account_roles(account_id,role)
                    SELECT id,? FROM accounts WHERE code=? AND is_group=0 AND active=1""", (role, code))
        conn.execute("""INSERT INTO management_dashboard_meta(meta_key,meta_value) VALUES('roles_initialized','1')
            ON CONFLICT(meta_key) DO UPDATE SET meta_value='1'""")
        conn.execute("""INSERT INTO management_dashboard_meta(meta_key,meta_value) VALUES('seed_activity',?)
            ON CONFLICT(meta_key) DO UPDATE SET meta_value=excluded.meta_value""", (activity,))
    # Add newly introduced chart roles for untouched default mappings without
    # overwriting user-customized role assignments on existing installations.
    if not customized:
        defaults = ACTIVITY_ACCOUNTS.get(activity, ACTIVITY_ACCOUNTS["commercial"])
        for role, codes in defaults.items():
            for code in codes:
                conn.execute("""INSERT OR IGNORE INTO management_account_roles(account_id,role)
                    SELECT id,? FROM accounts WHERE code=? AND is_group=0 AND active=1""", (role, code))
    conn.commit()


def load_management_account_roles(conn):
    """Read configured account-role mappings only; never create schema or seed data."""
    result = {role: set() for role in MANAGEMENT_ROLES}
    for row in conn.execute("SELECT account_id,role FROM management_account_roles"):
        if row["role"] in result:
            result[row["role"]].add(row["account_id"])
    return result


def _top_accounts(balances, account_type, value_key):
    rows = []
    for account_id, item in balances.items():
        if item["is_group"] or item["type"] != account_type:
            continue
        value = item["credit"] - item["debit"] if value_key == "credit" else item["debit"] - item["credit"]
        if value:
            rows.append({"id": account_id, "code": item["code"], "name": item["name"], "value": value})
    rows.sort(key=lambda x: abs(x["value"]), reverse=True)
    maximum = max((abs(x["value"]) for x in rows), default=Decimal("0"))
    for row in rows:
        row["width"] = float((abs(row["value"]) * Decimal("100") / maximum).quantize(Decimal("0.1"))) if maximum else 0
    return rows


def cached_management_snapshot(conn, start, end, dimensions=None):
    """Return a cached Decimal-safe snapshot, invalidated by DB-level triggers."""
    dimensions = dimensions or {}
    started_transaction = not conn.in_transaction
    if started_transaction:
        conn.execute("BEGIN")
    try:
        version_row = conn.execute(
            "SELECT version FROM management_dashboard_data_version WHERE id=1"
        ).fetchone()
        version = int(version_row["version"]) if version_row else 0
        role_fingerprint = tuple(
            (role, tuple(sorted(ids)))
            for role, ids in sorted(load_management_account_roles(conn).items())
        )
        settings_fingerprint = tuple(
            (row["setting_key"], row["setting_value"])
            for row in conn.execute("SELECT setting_key,setting_value FROM company_settings ORDER BY setting_key")
        ) if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='company_settings'"
        ).fetchone() else ()
        key = (
            version, start.isoformat(), end.isoformat(),
            tuple(sorted((str(k), str(v)) for k, v in dimensions.items())),
            role_fingerprint, settings_fingerprint,
        )
        with _SNAPSHOT_CACHE_LOCK:
            cached = _SNAPSHOT_CACHE.get(key)
            if cached is not None:
                _SNAPSHOT_CACHE.move_to_end(key)
                if started_transaction:
                    conn.commit()
                return deepcopy(cached)
        snapshot = build_management_snapshot(conn, start, end, dimensions)
        if started_transaction:
            conn.commit()
        with _SNAPSHOT_CACHE_LOCK:
            _SNAPSHOT_CACHE[key] = deepcopy(snapshot)
            _SNAPSHOT_CACHE.move_to_end(key)
            while len(_SNAPSHOT_CACHE) > _SNAPSHOT_CACHE_LIMIT:
                _SNAPSHOT_CACHE.popitem(last=False)
        return snapshot
    except Exception:
        if started_transaction:
            conn.rollback()
        raise


def build_management_snapshot(conn, start, end, dimensions=None):
    """Build a Decimal-safe management snapshot for inclusive dates."""
    if end < start:
        raise ValueError("تاريخ النهاية لازم يكون بعد تاريخ البداية.")
    dimensions = dimensions or {}
    settings = {}
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='company_settings'").fetchone():
        settings = {row["setting_key"]: row["setting_value"] for row in conn.execute(
            "SELECT setting_key,setting_value FROM company_settings WHERE setting_key IN ('activity','base_currency','company_name')"
        )}
    activity = settings.get("activity", "commercial")
    roles = load_management_account_roles(conn)
    period_days = (end - start).days
    previous_end = start - timedelta(days=1)
    previous_start = previous_end - timedelta(days=period_days)
    current_balances = account_balances(
        conn, start.isoformat(), end.isoformat(), dimensions, exclude_closing_entries=True
    )
    previous_balances = account_balances(
        conn, previous_start.isoformat(), previous_end.isoformat(), dimensions, exclude_closing_entries=True
    )
    as_of = account_balances(conn, date_to=end.isoformat(), dimensions=dimensions)
    current = income_figures(current_balances, direct_cost_ids=roles["direct_cost"])
    previous = income_figures(previous_balances, direct_cost_ids=roles["direct_cost"])
    cumulative = income_figures(as_of, direct_cost_ids=roles["direct_cost"])
    assets = _leaf_sum(as_of, "asset")
    liabilities = _leaf_sum(as_of, "liability")
    equity = _leaf_sum(as_of, "equity")
    balance_ok = (assets == liabilities + equity + cumulative["net_profit"]) if not any(dimensions.values()) else None
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
        "selected_dimensions": dimensions,
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


def cash_forecast(conn, today=None, weeks=8):
    today = today or date.today()
    roles = load_management_account_roles(conn)
    balances = account_balances(conn, date_to=today.isoformat())
    opening = _leaf_sum(balances, "asset", account_ids=roles["cash"])
    inflows = [Decimal("0") for _ in range(weeks)]
    outflows = [Decimal("0") for _ in range(weeks)]
    periods = []
    for index in range(weeks):
        start = today + timedelta(days=index * 7)
        end = start + timedelta(days=6)
        periods.append({"label": start.strftime("%d/%m") + " - " + end.strftime("%d/%m")})
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sales_invoices'").fetchone():
        rows = conn.execute("""SELECT total_amount,paid_amount,COALESCE(credited_amount,'0') credited,
            COALESCE(due_date,invoice_date) due_date FROM sales_invoices WHERE status='posted' AND invoice_date<=?""",
            (today.isoformat(),)).fetchall()
        for row in rows:
            amount = max(Decimal("0"), _decimal(row["total_amount"])-_decimal(row["paid_amount"])-_decimal(row["credited"]))
            if amount:
                try: due = date.fromisoformat(row["due_date"])
                except (TypeError,ValueError): due = today
                idx = max(0,(due-today).days//7)
                if idx < weeks: inflows[idx] += amount
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='supplier_bills'").fetchone():
        rows = conn.execute("""SELECT total_amount,paid_amount,COALESCE(credited_amount,'0') credited,
            COALESCE(due_date,bill_date) due_date FROM supplier_bills WHERE status='posted' AND bill_date<=?""",
            (today.isoformat(),)).fetchall()
        for row in rows:
            amount = max(Decimal("0"), _decimal(row["total_amount"])-_decimal(row["paid_amount"])-_decimal(row["credited"]))
            if amount:
                try: due = date.fromisoformat(row["due_date"])
                except (TypeError,ValueError): due = today
                idx = max(0,(due-today).days//7)
                if idx < weeks: outflows[idx] += amount
    payroll_due = Decimal("0")
    for row in conn.execute("SELECT id,name_ar FROM accounts WHERE account_type='liability' AND is_group=0 AND active=1"):
        if any(x in (row["name_ar"] or "").lower() for x in ("مرتبات","رواتب","salary","payroll")):
            payroll_due += max(Decimal("0"),_leaf_sum(balances,"liability",account_ids={row["id"]}))
    outflows[0] += payroll_due
    running = opening
    result = []
    for i, period in enumerate(periods):
        net = inflows[i]-outflows[i]
        start_balance = running
        running += net
        result.append({**period,"opening":_rounded(start_balance),"receipts":_rounded(inflows[i]),
            "payments":_rounded(outflows[i]),"net":_rounded(net),"closing":_rounded(running)})
    threshold_row = conn.execute("SELECT setting_value FROM company_settings WHERE setting_key='minimum_cash_balance'").fetchone()
    threshold = _decimal(threshold_row["setting_value"]) if threshold_row else Decimal("0")
    return {"opening":_rounded(opening),"weeks":result,"minimum":threshold,
        "warning":any(x["closing"]<threshold for x in result) if threshold>0 else False,
        "payroll_due":_rounded(payroll_due)}


def unusual_journal_entries(conn, period_start, period_end, multiplier=Decimal("3")):
    """Find unusual posted entries without per-entry or per-line SQL queries.

    Historical averages are calculated with Decimal prefix sums in Python.
    The database work is bounded to three data queries regardless of the
    number of candidate entries.
    """
    from bisect import bisect_left

    register_decimal_aggregates(conn)
    columns = {x["name"] for x in conn.execute("PRAGMA table_info(journal_entries)")}
    ref_expr = "je.reference" if "reference" in columns else "''"
    desc_expr = "je.description" if "description" in columns else "''"
    entries = conn.execute(f"""SELECT je.id,je.entry_date,{ref_expr} reference,{desc_expr} description,
        COALESCE(DECIMAL_SUM(jl.debit),'0') debit,COALESCE(DECIMAL_SUM(jl.credit),'0') credit
        FROM journal_entries je JOIN journal_lines jl ON jl.entry_id=je.id
        WHERE je.status='posted' AND je.entry_date BETWEEN ? AND ?
        GROUP BY je.id ORDER BY je.entry_date DESC,je.id DESC""",
        (period_start.isoformat(),period_end.isoformat())).fetchall()
    if not entries:
        return []

    candidate_lines = conn.execute("""SELECT jl.id line_id,jl.entry_id,jl.account_id,
        jl.debit,jl.credit,a.code,a.name_ar
        FROM journal_lines jl
        JOIN journal_entries je ON je.id=jl.entry_id
        JOIN accounts a ON a.id=jl.account_id
        WHERE je.status='posted' AND je.entry_date BETWEEN ? AND ?
        ORDER BY jl.entry_id,jl.id""",
        (period_start.isoformat(),period_end.isoformat())).fetchall()
    account_ids = sorted({row["account_id"] for row in candidate_lines})
    history_by_account = {}
    if account_ids:
        placeholders = ",".join("?" for _ in account_ids)
        history_start = (period_start - timedelta(days=90)).isoformat()
        history_end = (period_end + timedelta(days=1)).isoformat()
        history_rows = conn.execute(f"""SELECT h.account_id,e.entry_date,h.debit,h.credit
            FROM journal_lines h
            JOIN journal_entries e ON e.id=h.entry_id
            WHERE e.status='posted' AND h.account_id IN ({placeholders})
              AND e.entry_date>=? AND e.entry_date<?
            ORDER BY h.account_id,e.entry_date,h.id""",
            (*account_ids, history_start, history_end)).fetchall()
        for row in history_rows:
            bucket = history_by_account.setdefault(row["account_id"], {"dates": [], "prefix": [Decimal("0")]})
            amount = max(_decimal(row["debit"]), _decimal(row["credit"]))
            bucket["dates"].append(row["entry_date"])
            bucket["prefix"].append(bucket["prefix"][-1] + amount)

    lines_by_entry = {}
    for line in candidate_lines:
        lines_by_entry.setdefault(line["entry_id"], []).append(line)

    threshold = Decimal(str(multiplier))
    result = []
    for entry in entries:
        entry_date = date.fromisoformat(entry["entry_date"])
        reasons = []
        if entry_date >= period_end - timedelta(days=1):
            reasons.append("قيد في آخر يومين من الفترة")
        lower_date = (entry_date - timedelta(days=90)).isoformat()
        current_date = entry_date.isoformat()
        for line in lines_by_entry.get(entry["id"], ()):
            history = history_by_account.get(line["account_id"])
            if not history:
                continue
            dates = history["dates"]
            prefix = history["prefix"]
            lower = bisect_left(dates, lower_date)
            upper = bisect_left(dates, current_date)
            count = upper - lower
            if count <= 0:
                continue
            historical_total = prefix[upper] - prefix[lower]
            average = historical_total / Decimal(count)
            amount = max(_decimal(line["debit"]), _decimal(line["credit"]))
            if average > 0 and amount >= average * threshold:
                reasons.append("مبلغ مرتفع عن متوسط حساب "+line["code"]+" — "+line["name_ar"])
        if reasons:
            result.append({"id":entry["id"],"date":entry["entry_date"],"reference":entry["reference"] or "",
                "description":entry["description"] or "","debit":_decimal(entry["debit"]),
                "credit":_decimal(entry["credit"]),"reasons":reasons})
    return result
