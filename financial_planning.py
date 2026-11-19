"""Budget planning and cash-flow reporting for NEXUS ERP."""
from app_paths import DB_PATH
from date_formatting import format_export_dates
from database import connect_database
from db_compat import (
    database_dialect, table_columns, table_exists, execute_script,
    begin_write_transaction, insert_and_get_id, transaction_is_active,
    DB_INTEGRITY_ERROR, DB_ERROR, transactional_migration,
)
from db_sql import execute_compat, executemany_compat


def _execute(conn, sql, params=()):
    return execute_compat(conn, sql, params)


def _executemany(conn, sql, rows):
    return executemany_compat(conn, sql, rows)
from accounting_core import (post_journal, register_decimal_aggregates, reconciliation_snapshot,
                             decimal_sum_expression, decimal_net_expression,
                             month_key_expression)
from chart_templates import ACTIVITY_ACCOUNT_ROLES
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import wraps
from flask import Blueprint, Response, abort, flash, redirect, render_template, request, send_file, session, url_for
import io

planning_bp = Blueprint("planning", __name__)
CENT = Decimal("0.01")


def _money(value):
    try:
        amount = Decimal(str(value or "0"))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("أدخل مبلغًا رقميًا موجبًا أو صفرًا.")
    if not amount.is_finite() or amount < 0:
        raise ValueError("أدخل مبلغًا رقميًا موجبًا أو صفرًا.")
    return amount.quantize(CENT, rounding=ROUND_HALF_UP)

def _signed_money(value):
    try:
        amount = Decimal(str(value or "0"))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("أدخل رصيدًا رقميًا صحيحًا.")
    if not amount.is_finite():
        raise ValueError("أدخل رصيدًا رقميًا صحيحًا.")
    return amount.quantize(CENT, rounding=ROUND_HALF_UP)


def _budget_months(date_from, date_to):
    from datetime import date
    start = date.fromisoformat(date_from)
    end = date.fromisoformat(date_to)
    if end < start:
        raise ValueError("فترة الموازنة غير صحيحة.")
    months = []
    current = date(start.year, start.month, 1)
    last = date(end.year, end.month, 1)
    while current <= last:
        months.append(current.strftime("%Y-%m"))
        current = date(current.year + (current.month == 12), 1 if current.month == 12 else current.month + 1, 1)
    return months


def _split_monthly_amount(total, months):
    if not months:
        raise ValueError("لا توجد أشهر داخل فترة الموازنة.")
    total = _money(total)
    share = (total / Decimal(len(months))).quantize(CENT, rounding=ROUND_HALF_UP)
    values = [share] * len(months)
    values[-1] = total - sum(values[:-1], Decimal("0"))
    return values


def _budget_audit(conn, budget_id, action, actor_id, details=""):
    _execute(
        conn,
        "INSERT INTO budget_audit_log(budget_id,action,actor_id,details) VALUES(?,?,?,?)",
        (budget_id, action, actor_id, str(details or "")[:1000])
    )


@transactional_migration
def initialize_planning_schema(conn):
    execute_script(conn, """
    CREATE TABLE IF NOT EXISTS budget_plans(
      id INTEGER PRIMARY KEY, name TEXT NOT NULL, date_from TEXT NOT NULL,
      date_to TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
      created_by INTEGER, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS budget_lines(
      id INTEGER PRIMARY KEY, budget_id INTEGER NOT NULL REFERENCES budget_plans(id) ON DELETE CASCADE,
      account_id INTEGER NOT NULL REFERENCES accounts(id), amount TEXT NOT NULL,
      UNIQUE(budget_id,account_id)
    );
    CREATE INDEX IF NOT EXISTS idx_budget_period ON budget_plans(date_from,date_to,status);
    CREATE TABLE IF NOT EXISTS budget_monthly_lines(
      id INTEGER PRIMARY KEY,
      budget_id INTEGER NOT NULL REFERENCES budget_plans(id) ON DELETE CASCADE,
      account_id INTEGER NOT NULL REFERENCES accounts(id),
      period TEXT NOT NULL,
      amount TEXT NOT NULL,
      UNIQUE(budget_id,account_id,period)
    );
    CREATE INDEX IF NOT EXISTS idx_budget_monthly_period ON budget_monthly_lines(budget_id,period,account_id);
    CREATE TABLE IF NOT EXISTS budget_audit_log(
      id INTEGER PRIMARY KEY, budget_id INTEGER NOT NULL REFERENCES budget_plans(id),
      action TEXT NOT NULL, actor_id INTEGER, details TEXT,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_budget_audit_history ON budget_audit_log(budget_id,id);
    CREATE TABLE IF NOT EXISTS cash_flow_account_mappings(
      account_id INTEGER PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
      category TEXT NOT NULL CHECK(category IN ('operating','investing','financing')),
      updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_cash_flow_mapping_category
      ON cash_flow_account_mappings(category);
    CREATE TABLE IF NOT EXISTS financial_years(
      id INTEGER PRIMARY KEY,
      year_name TEXT NOT NULL UNIQUE,
      start_date TEXT NOT NULL,
      end_date TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','closing','closed')),
      opening_entry_id INTEGER REFERENCES journal_entries(id),
      closing_entry_id INTEGER REFERENCES journal_entries(id),
      created_by INTEGER,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      CHECK(start_date <= end_date)
    );
    CREATE INDEX IF NOT EXISTS idx_financial_year_dates
      ON financial_years(start_date,end_date,status);
    CREATE TABLE IF NOT EXISTS financial_year_journal_history(
      id INTEGER PRIMARY KEY,
      financial_year_id INTEGER NOT NULL REFERENCES financial_years(id),
      entry_kind TEXT NOT NULL CHECK(entry_kind IN ('opening','closing')),
      action TEXT NOT NULL CHECK(action IN ('created','reversed')),
      entry_id INTEGER NOT NULL UNIQUE REFERENCES journal_entries(id),
      related_entry_id INTEGER REFERENCES journal_entries(id),
      created_by INTEGER,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_financial_year_journal_history
      ON financial_year_journal_history(financial_year_id,entry_kind,id);
    """)
    # Freeze whether a later-year opening entry duplicates already-posted
    # historical ledger activity. A future backfill must never flip this flag.
    history_columns = table_columns(conn, "financial_year_journal_history")
    if "ledger_history_copied" not in history_columns:
        _execute(
            conn,
            "ALTER TABLE financial_year_journal_history ADD COLUMN ledger_history_copied INTEGER"
        )
    if database_dialect(conn) == "sqlite":
        execute_script(conn, """
        CREATE TRIGGER IF NOT EXISTS budget_audit_no_update
          BEFORE UPDATE ON budget_audit_log
          BEGIN SELECT RAISE(ABORT,'Budget audit history is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS budget_audit_no_delete
          BEFORE DELETE ON budget_audit_log
          BEGIN SELECT RAISE(ABORT,'Budget audit history is immutable'); END;
        """)
    else:
        execute_script(conn, """
        CREATE OR REPLACE FUNCTION nexus_reject_budget_audit_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $nexus$
        BEGIN
          RAISE EXCEPTION 'Budget audit history is immutable';
        END;
        $nexus$;
        DROP TRIGGER IF EXISTS budget_audit_no_update ON budget_audit_log;
        DROP TRIGGER IF EXISTS budget_audit_no_delete ON budget_audit_log;
        CREATE TRIGGER budget_audit_no_update
          BEFORE UPDATE ON budget_audit_log
          FOR EACH ROW EXECUTE FUNCTION nexus_reject_budget_audit_mutation();
        CREATE TRIGGER budget_audit_no_delete
          BEFORE DELETE ON budget_audit_log
          FOR EACH ROW EXECUTE FUNCTION nexus_reject_budget_audit_mutation();
        """)

    budget_columns = table_columns(conn, "budget_plans")
    for column in ("branch_id", "cost_center_id", "project_id"):
        if column not in budget_columns:
            _execute(conn, f"ALTER TABLE budget_plans ADD COLUMN {column} INTEGER")
    _execute(conn, "CREATE INDEX IF NOT EXISTS idx_budget_dimensions ON budget_plans(branch_id,cost_center_id,project_id)")
    budget_columns = table_columns(conn, "budget_plans")
    for column, declaration in (("submitted_by", "INTEGER"), ("submitted_at", "TEXT"),
                                ("approved_by", "INTEGER"), ("approved_at", "TEXT"),
                                ("rejection_reason", "TEXT")):
        if column not in budget_columns:
            _execute(conn, f"ALTER TABLE budget_plans ADD COLUMN {column} {declaration}")
    _execute(conn, "UPDATE budget_plans SET status='approved' WHERE status='active'")
    plans = _execute(conn, "SELECT id,date_from,date_to FROM budget_plans").fetchall()
    for plan in plans:
        budget_id, date_from, date_to = plan["id"], plan["date_from"], plan["date_to"]
        existing = _execute(conn, "SELECT 1 FROM budget_monthly_lines WHERE budget_id=? LIMIT 1", (budget_id,)).fetchone()
        if existing:
            continue
        months = _budget_months(date_from, date_to)
        lines = _execute(conn, "SELECT account_id,amount FROM budget_lines WHERE budget_id=?", (budget_id,)).fetchall()
        for line in lines:
            allocations = _split_monthly_amount(_money(line["amount"]), months)
            _executemany(
                conn,
                "INSERT INTO budget_monthly_lines(budget_id,account_id,period,amount) VALUES(?,?,?,?) "
                "ON CONFLICT(budget_id,account_id,period) DO NOTHING",
                [(budget_id, line["account_id"], month, str(amount))
                 for month, amount in zip(months, allocations)]
            )
    if table_exists(conn, "journal_entries"):
        if "entry_type" not in table_columns(conn, "journal_entries"):
            _execute(conn, "ALTER TABLE journal_entries ADD COLUMN entry_type TEXT NOT NULL DEFAULT 'general'")
        _execute(conn, """UPDATE journal_entries SET entry_type='year_end_closing'
            WHERE id IN (SELECT entry_id FROM financial_year_journal_history
                         WHERE entry_kind='closing' AND action='created')""")
        _execute(conn, """UPDATE journal_entries SET entry_type='year_end_closing_reversal'
            WHERE id IN (SELECT entry_id FROM financial_year_journal_history
                         WHERE entry_kind='closing' AND action='reversed')""")
    conn.commit()


def create_financial_year(conn, year_name, start_date, end_date, created_by=None):
    """Create a non-overlapping financial year; never changes journal data."""
    from datetime import date
    name = str(year_name or "").strip()
    if not name or len(name) > 80:
        raise ValueError("أدخل اسمًا صحيحًا للسنة المالية.")
    try:
        start = date.fromisoformat(str(start_date)).isoformat()
        end = date.fromisoformat(str(end_date)).isoformat()
    except (TypeError, ValueError):
        raise ValueError("تواريخ السنة المالية غير صحيحة؛ استخدم YYYY-MM-DD.")
    if end < start:
        raise ValueError("تاريخ نهاية السنة يجب ألا يسبق تاريخ بدايتها.")
    begin_write_transaction(conn)
    try:
        overlap = _execute(conn, 
            """SELECT year_name FROM financial_years
               WHERE start_date <= ? AND end_date >= ? LIMIT 1""",
            (end, start),
        ).fetchone()
        if overlap:
            overlapping_name = overlap["year_name"] if isinstance(overlap, dict) else overlap[0]
            raise ValueError(f"الفترة تتداخل مع السنة المالية: {overlapping_name}.")
        year_id = insert_and_get_id(conn,
            """INSERT INTO financial_years(year_name,start_date,end_date,created_by)
               VALUES(?,?,?,?)""",
            (name, start, end, created_by),
        )
        conn.commit()
        return year_id
    except Exception:
        conn.rollback()
        raise


def list_financial_years(conn):
    """Return financial years in descending chronological order."""
    return _execute(conn, 
        "SELECT * FROM financial_years ORDER BY start_date DESC,id DESC"
    ).fetchall()

def _start_financial_year_transaction(conn):
    if transaction_is_active(conn):
        raise ValueError("هناك عملية قاعدة بيانات مفتوحة؛ احفظها أو أغلقها قبل تنفيذ الإجراء.")
    begin_write_transaction(conn)


def _financial_year_or_error(conn, year_id):
    year = _execute(conn, "SELECT * FROM financial_years WHERE id=?", (year_id,)).fetchone()
    if not year:
        raise ValueError("السنة المالية غير موجودة.")
    return year


def _history_sequence(conn, year_id, entry_kind, action):
    row = _execute(conn, """SELECT COUNT(*) FROM financial_year_journal_history
        WHERE financial_year_id=? AND entry_kind=? AND action=?""",
        (year_id, entry_kind, action)).fetchone()
    return int(row[0]) + 1


def _previous_closed_financial_year(conn, start_date):
    return _execute(conn, """SELECT * FROM financial_years
        WHERE end_date < ? ORDER BY end_date DESC,id DESC LIMIT 1""",
        (start_date,)).fetchone()


def _opening_balance_mismatches(conn, cutoff_date, normalized):
    """Compare opening balances with the ledger at the day before the new year starts.

    Any unclosed prior-period profit/loss is included in the retained-earnings
    account, so an unclosed previous financial year cannot bypass validation.
    """
    account_rows = _execute(conn, """SELECT id,code,name_ar,account_type FROM accounts
        WHERE account_type IN ('asset','liability','equity') AND is_group=0""").fetchall()
    expected_balances = {row["id"]: Decimal("0") for row in account_rows}
    metadata = {row["id"]: row for row in account_rows}
    from management_dashboard import _copied_opening_entry_ids
    excluded_openings = sorted(_copied_opening_entry_ids(conn, cutoff_date))
    opening_exclusion = (" AND je.id NOT IN (" + ",".join("?" for _ in excluded_openings) + ")"
                         if excluded_openings else "")
    opening_params = (cutoff_date, *excluded_openings)
    rows = _execute(conn, f"""SELECT jl.account_id,a.account_type,jl.debit,jl.credit
        FROM journal_lines jl
        JOIN journal_entries je ON je.id=jl.entry_id
        JOIN accounts a ON a.id=jl.account_id
        WHERE je.status='posted' AND je.entry_date<=?{opening_exclusion}
          AND a.account_type IN ('asset','liability','equity') AND a.is_group=0
        ORDER BY jl.account_id,je.id,jl.id""", opening_params).fetchall()
    for row in rows:
        account_id = row["account_id"]
        debit = Decimal(str(row["debit"] or "0"))
        credit = Decimal(str(row["credit"] or "0"))
        expected_balances[account_id] += (
            debit - credit if row["account_type"] == "asset" else credit - debit
        )

    # If prior P&L has not yet been closed, carry its net result into retained earnings
    # for comparison with the opening balance entered for the new financial year.
    pnl_rows = _execute(conn, f"""SELECT a.account_type,jl.debit,jl.credit
        FROM journal_lines jl
        JOIN journal_entries je ON je.id=jl.entry_id
        JOIN accounts a ON a.id=jl.account_id
        WHERE je.status='posted' AND je.entry_date<=?{opening_exclusion}
          AND a.account_type IN ('revenue','expense') AND a.is_group=0""",
        opening_params).fetchall()
    prior_net_result = Decimal("0")
    for row in pnl_rows:
        debit = Decimal(str(row["debit"] or "0"))
        credit = Decimal(str(row["credit"] or "0"))
        prior_net_result += credit - debit
    if prior_net_result:
        retained = _retained_earnings_account(conn)
        retained_id = retained["id"]
        expected_balances[retained_id] = expected_balances.get(retained_id, Decimal("0")) + prior_net_result
        if retained_id not in metadata:
            account = _execute(conn, "SELECT id,code,name_ar,account_type FROM accounts WHERE id=?",
                                   (retained_id,)).fetchone()
            if account:
                metadata[retained_id] = account

    mismatches = []
    for account_id in set(expected_balances) | set(normalized):
        expected = expected_balances.get(account_id, Decimal("0")).quantize(CENT)
        supplied = normalized.get(account_id, Decimal("0")).quantize(CENT)
        if expected != supplied:
            account = metadata.get(account_id)
            code = account["code"] if account else str(account_id)
            name = account["name_ar"] if account else ""
            mismatches.append((code, name, expected, supplied))
    return mismatches


def _retained_earnings_account(conn):
    activity = "commercial"
    if _table_exists_financial(conn, "company_settings"):
        row = _execute(conn, """SELECT setting_value FROM company_settings
            WHERE setting_key='activity' LIMIT 1""").fetchone()
        if row and row["setting_value"] in ACTIVITY_ACCOUNT_ROLES:
            activity = row["setting_value"]
    role_codes = ACTIVITY_ACCOUNT_ROLES.get(activity, {}).get("retained_earnings", ())
    if isinstance(role_codes, str):
        role_codes = (role_codes,)
    for code in role_codes:
        account = _execute(conn, """SELECT id FROM accounts
            WHERE code=? AND account_type='equity' AND is_group=0 AND active=1 LIMIT 1""",
            (code,)).fetchone()
        if account:
            return account
    raise ValueError(f"لم يتم العثور على حساب تفصيلي نشط لدور الأرباح المحتجزة للنشاط ({activity}).")


def create_opening_entry(conn, year_id, balances, created_by=None):
    """Post one balanced opening journal using balance-sheet accounts only."""
    _start_financial_year_transaction(conn)
    try:
        year = _financial_year_or_error(conn, year_id)
        if year["status"] != "open":
            raise ValueError("لا يمكن إنشاء قيد افتتاح لسنة غير مفتوحة.")
        if year["opening_entry_id"]:
            raise ValueError("تم إنشاء قيد افتتاح لهذه السنة بالفعل. صححه بعكس القيد أولًا.")
        posted_before_opening = _execute(conn, """SELECT COUNT(*) FROM journal_entries
            WHERE status='posted' AND entry_date>=? AND entry_date<=?
              AND id NOT IN (SELECT entry_id FROM financial_year_journal_history
                  WHERE financial_year_id=? AND entry_kind='opening')""",
            (year["start_date"], year["end_date"], year_id)).fetchone()[0]
        if posted_before_opening:
            raise ValueError(
                f"لا يمكن إنشاء قيد الافتتاح؛ توجد {posted_before_opening} قيود مرحلة داخل السنة. "
                "يجب أن يكون قيد الافتتاح أول قيد مرحّل في السنة المالية."
            )
        if not isinstance(balances, dict) or not balances:
            raise ValueError("أدخل أرصدة حسابات الميزانية.")
        normalized = {}
        for raw_id, raw_amount in balances.items():
            try:
                account_id = int(raw_id)
            except (TypeError, ValueError):
                raise ValueError("يوجد رقم حساب غير صحيح في أرصدة الافتتاح.")
            if account_id <= 0 or account_id in normalized:
                raise ValueError("يوجد حساب مكرر أو غير صحيح.")
            normalized[account_id] = _signed_money(raw_amount)
        marks = ",".join("?" for _ in normalized)
        accounts = _execute(conn, 
            f"SELECT id,code,name_ar,account_type,is_group,active FROM accounts WHERE id IN ({marks})",
            tuple(normalized),
        ).fetchall()
        if len(accounts) != len(normalized):
            raise ValueError("أحد حسابات الافتتاح غير موجود.")
        totals = {"asset": Decimal("0"), "liability": Decimal("0"), "equity": Decimal("0")}
        lines = []
        for account in accounts:
            kind = account["account_type"]
            if kind not in totals or account["is_group"] or not account["active"]:
                raise ValueError("قيد الافتتاح يقبل الحسابات التفصيلية النشطة للأصول والالتزامات وحقوق الملكية فقط.")
            amount = normalized[account["id"]]
            totals[kind] += amount
            if amount:
                if kind == "asset":
                    debit, credit = max(amount, Decimal("0")), max(-amount, Decimal("0"))
                else:
                    debit, credit = max(-amount, Decimal("0")), max(amount, Decimal("0"))
                lines.append((account["id"], debit, credit, "رصيد افتتاحي"))
        if totals["asset"] != totals["liability"] + totals["equity"]:
            raise ValueError(
                "قيد الافتتاح غير متوازن: الأصول "
                f"{totals['asset']}، والالتزامات وحقوق الملكية "
                f"{totals['liability'] + totals['equity']}."
            )
        if not lines:
            raise ValueError("لا يمكن ترحيل قيد افتتاح بدون أرصدة فعلية.")
        previous_year = _previous_closed_financial_year(conn, year["start_date"])
        prior_posted = _execute(conn, """SELECT COUNT(*) FROM journal_entries
            WHERE status='posted' AND entry_date<?""", (year["start_date"],)).fetchone()[0]
        # Validate whenever a prior financial-year record OR any prior posted
        # ledger activity exists. This also covers legacy companies with no
        # financial_years row for their historical balances.
        if previous_year or prior_posted:
            from datetime import date, timedelta
            cutoff_date = (date.fromisoformat(year["start_date"]) - timedelta(days=1)).isoformat()
            mismatches = _opening_balance_mismatches(conn, cutoff_date, normalized)
            if mismatches:
                details = "؛ ".join(
                    f"{code} {name}: الرصيد الدفتري المتوقع {expected} مقابل رصيد الافتتاح {supplied}"
                    for code, name, expected, supplied in mismatches[:8]
                )
                suffix = f" (ويوجد {len(mismatches) - 8} اختلافات أخرى)" if len(mismatches) > 8 else ""
                period_label = (
                    f"حتى {cutoff_date} بعد السنة السابقة ({previous_year['year_name']})"
                    if previous_year else f"حتى {cutoff_date} من أرصدة الشركة التاريخية"
                )
                raise ValueError(
                    f"أرصدة الافتتاح لا تطابق الأرصدة الدفترية {period_label}: "
                    f"{details}{suffix}."
                )
        sequence = _history_sequence(conn, year_id, "opening", "created")
        reference = f"FY-{year_id}-OPEN-{sequence}"
        entry_id = post_journal(
            conn, year["start_date"], reference,
            f"قيد الأرصدة الافتتاحية للسنة المالية {year['year_name']}",
            lines, created_by,
        )
        _execute(conn, "UPDATE financial_years SET opening_entry_id=? WHERE id=?", (entry_id, year_id))
        history_columns = table_columns(conn, "financial_year_journal_history")
        if "ledger_history_copied" in history_columns:
            _execute(conn, """INSERT INTO financial_year_journal_history
                (financial_year_id,entry_kind,action,entry_id,created_by,ledger_history_copied)
                VALUES(?,'opening','created',?,?,?)""",
                (year_id, entry_id, created_by, int(prior_posted > 0)))
        else:
            _execute(conn, """INSERT INTO financial_year_journal_history
                (financial_year_id,entry_kind,action,entry_id,created_by)
                VALUES(?,'opening','created',?,?)""", (year_id, entry_id, created_by))
        conn.commit()
        return entry_id
    except Exception:
        conn.rollback()
        raise


def build_financial_year_preclose_report(conn, year_id):
    """Read-only accounting and operational checks before annual closing."""
    year = _financial_year_or_error(conn, year_id)
    start, end = year["start_date"], year["end_date"]
    checks = []

    def add(code, title, severity, count, details, blocking=False):
        checks.append({"code": code, "title": title, "severity": severity,
                       "count": int(count or 0), "details": details,
                       "blocking": bool(blocking)})

    drafts = _execute(conn, """SELECT COUNT(*) FROM journal_entries
        WHERE status='draft' AND entry_date>=? AND entry_date<=?""", (start, end)).fetchone()[0]
    add("draft_entries", "قيود مسودة داخل السنة", "danger" if drafts else "success",
        drafts, "يجب معالجة القيود المسودة قبل الإقفال.", drafts > 0)

    register_decimal_aggregates(conn)
    grouped_totals = _execute(conn, f"""SELECT je.id,
            {decimal_sum_expression(conn, "jl.debit")} debit,
            {decimal_sum_expression(conn, "jl.credit")} credit
        FROM journal_entries je LEFT JOIN journal_lines jl ON jl.entry_id=je.id
        WHERE je.status='posted' AND je.entry_date>=? AND je.entry_date<=?
        GROUP BY je.id""", (start, end)).fetchall()
    unbalanced = sum(
        Decimal(str(row["debit"] or "0")) != Decimal(str(row["credit"] or "0"))
        for row in grouped_totals
    )
    add("unbalanced_entries", "قيود مرحلة غير متوازنة", "danger" if unbalanced else "success",
        unbalanced, "راجع القيود وصححها بقيود تسوية معتمدة.", unbalanced > 0)

    empty = _execute(conn, """SELECT COUNT(*) FROM journal_entries je
        WHERE je.status='posted' AND je.entry_date>=? AND je.entry_date<=?
          AND NOT EXISTS (SELECT 1 FROM journal_lines jl WHERE jl.entry_id=je.id)""",
        (start, end)).fetchone()[0]
    add("empty_entries", "قيود مرحلة بلا سطور", "danger" if empty else "success",
        empty, "كل قيد مرحّل يجب أن يحتوي على سطور.", empty > 0)

    has_opening = bool(year["opening_entry_id"])
    add("opening_entry", "قيد افتتاح السنة", "success" if has_opening else "warning",
        0 if has_opening else 1,
        "قيد الافتتاح مرتبط بالسنة." if has_opening else
        "لا يوجد قيد افتتاح مرتبط؛ راجع ما إذا كانت الأرصدة الافتتاحية مطلوبة.", False)
    if has_opening:
        opening = _execute(conn, f"""SELECT je.entry_date,je.status,
                COUNT(jl.id) line_count,
                {decimal_sum_expression(conn, "jl.debit")} debit,
                {decimal_sum_expression(conn, "jl.credit")} credit,
                COALESCE(SUM(CASE WHEN a.id IS NULL OR a.account_type NOT IN ('asset','liability','equity')
                                  OR a.is_group=1 OR a.active=0 THEN 1 ELSE 0 END),0) invalid_lines
            FROM journal_entries je
            LEFT JOIN journal_lines jl ON jl.entry_id=je.id
            LEFT JOIN accounts a ON a.id=jl.account_id
            WHERE je.id=? GROUP BY je.id""", (year["opening_entry_id"],)).fetchone()
        opening_valid = bool(
            opening and opening["status"] == "posted"
            and opening["entry_date"] == start
            and int(opening["line_count"] or 0) >= 2
            and Decimal(str(opening["debit"] or "0")) == Decimal(str(opening["credit"] or "0"))
            and int(opening["invalid_lines"] or 0) == 0
        )
        add("opening_integrity", "سلامة قيد الافتتاح وتاريخه", "success" if opening_valid else "danger",
            0 if opening_valid else 1,
            "القيد مرحّل ومتوازن وتاريخه يطابق بداية السنة ويستخدم حسابات ميزانية تفصيلية نشطة."
            if opening_valid else
            "قيد الافتتاح غير موجود أو غير مرحّل أو غير متوازن أو تاريخه/حساباته غير صحيحة.",
            not opening_valid)

        # A balanced opening journal can still contain wrong account balances.
        # Compare each opening line with the prior posted ledger before allowing year close.
        if opening_valid:
            from datetime import date, timedelta
            cutoff_date = (date.fromisoformat(start) - timedelta(days=1)).isoformat()
            prior_posted = _execute(conn, 
                "SELECT COUNT(*) FROM journal_entries WHERE status='posted' AND entry_date<?",
                (start,),
            ).fetchone()[0]
            previous_year = _previous_closed_financial_year(conn, start)
            if previous_year or prior_posted:
                opening_rows = _execute(conn, """SELECT jl.account_id,a.account_type,
                        jl.debit,jl.credit
                    FROM journal_lines jl
                    JOIN accounts a ON a.id=jl.account_id
                    WHERE jl.entry_id=? ORDER BY jl.account_id,jl.id""",
                    (year["opening_entry_id"],)).fetchall()
                supplied = {}
                for line in opening_rows:
                    account_id = line["account_id"]
                    amount = (Decimal(str(line["debit"] or "0")) -
                              Decimal(str(line["credit"] or "0")))
                    if line["account_type"] != "asset":
                        amount = -amount
                    supplied[account_id] = supplied.get(account_id, Decimal("0")) + amount
                mismatches = _opening_balance_mismatches(conn, cutoff_date, supplied)
                mismatch_details = "؛ ".join(
                    f"{code} {name}: الدفتري {expected:.2f} مقابل الافتتاح {actual:.2f}"
                    for code, name, expected, actual in mismatches[:8]
                )
                if len(mismatches) > 8:
                    mismatch_details += f"؛ واختلافات أخرى عددها {len(mismatches) - 8}"
                add("opening_balance_reconciliation", "مطابقة أرصدة الافتتاح مع الأستاذ",
                    "danger" if mismatches else "success", len(mismatches),
                    mismatch_details if mismatches else
                    f"أرصدة قيد الافتتاح تطابق الأستاذ حتى {cutoff_date}.",
                    bool(mismatches))

    invalid_account_lines = _execute(conn, """SELECT COUNT(*) FROM journal_lines jl
        JOIN journal_entries je ON je.id=jl.entry_id
        JOIN accounts a ON a.id=jl.account_id
        WHERE je.status='posted' AND je.entry_date>=? AND je.entry_date<=?
          AND (a.is_group=1 OR a.active=0)""", (start, end)).fetchone()[0]
    add("invalid_account_lines", "حركات على حسابات تجميعية أو غير نشطة",
        "danger" if invalid_account_lines else "success", invalid_account_lines,
        "يجب تصحيح الحركات المرتبطة بحساب تجميعي أو غير نشط قبل الإقفال.",
        invalid_account_lines > 0)

    if _table_exists_financial(conn, "accounting_periods"):
        locked_period = _execute(conn, """SELECT period_name FROM accounting_periods
            WHERE locked=1 AND start_date<=? AND end_date>=? LIMIT 1""", (end, end)).fetchone()
        add("year_end_period", "الفترة المحاسبية في تاريخ نهاية السنة",
            "danger" if locked_period else "success", 1 if locked_period else 0,
            f"الفترة {locked_period['period_name']} مغلقة بالفعل في تاريخ نهاية السنة." if locked_period else
            "لا توجد فترة مقفولة تمنع إجراء الإقفال.", bool(locked_period))

    if _table_exists_financial(conn, "bank_transactions"):
        bank_columns = table_columns(conn, "bank_transactions")
        if {"transaction_date", "reconciled"}.issubset(bank_columns):
            unreconciled_bank = _execute(conn, """SELECT COUNT(*) FROM bank_transactions
                WHERE transaction_date>=? AND transaction_date<=? AND reconciled=0""",
                (start, end)).fetchone()[0]
            add("bank_reconciliation", "حركات بنكية غير مسوّاة", "warning" if unreconciled_bank else "success",
                unreconciled_bank, "راجع المطابقة البنكية للحركات داخل السنة قبل اعتماد الإقفال.", False)

    # Fixed-asset depreciation must have a posted journal for every recorded run.
    if _table_exists_financial(conn, "asset_depreciations"):
        depreciation_columns = table_columns(conn, "asset_depreciations")
        if {"period", "journal_entry_id"}.issubset(depreciation_columns):
            unposted_depreciation = _execute(conn, """SELECT COUNT(*) FROM asset_depreciations d
                LEFT JOIN journal_entries je ON je.id=d.journal_entry_id
                WHERE d.period>=? AND d.period<=?
                  AND (d.journal_entry_id IS NULL OR je.status!='posted')""",
                (start[:7], end[:7])).fetchone()[0]
            add("asset_depreciation_integrity", "سلامة قيود إهلاك الأصول",
                "danger" if unposted_depreciation else "success", unposted_depreciation,
                "توجد سجلات إهلاك بلا قيد مرحّل أو مرتبطة بقيد غير مرحّل؛ عالجها قبل الإقفال."
                if unposted_depreciation else
                "كل سجلات الإهلاك المسجلة داخل السنة مرتبطة بقيود مرحلة.",
                bool(unposted_depreciation))
            if _table_exists_financial(conn, "fixed_assets"):
                no_run_assets = _execute(conn, """SELECT COUNT(*) FROM fixed_assets f
                    WHERE f.ready_date<=? AND f.status='active'
                      AND NOT EXISTS (SELECT 1 FROM asset_depreciations d
                        WHERE d.asset_id=f.id AND d.period>=? AND d.period<=?)""",
                    (end, start[:7], end[:7])).fetchone()[0]
                add("asset_depreciation_coverage", "مراجعة تشغيل إهلاك الأصول خلال السنة",
                    "warning" if no_run_assets else "success", no_run_assets,
                    f"عدد الأصول النشطة التي لا يظهر لها تشغيل إهلاك داخل السنة: {no_run_assets}. "
                    "راجع سجل كل أصل؛ قد يكون الأصل مستهلكًا بالكامل أو غير مستحق للإهلاك.",
                    False)

    # Revalue foreign-currency receivables/payables that remained open at year end.
    # This intentionally does not claim coverage of foreign-currency cash/bank balances
    # that are not registered in the FX transaction subledger.
    if (_table_exists_financial(conn, "fx_transactions")
            and _table_exists_financial(conn, "fx_revaluations")):
        try:
            settlement_rows = _execute(conn, """SELECT t.id,t.original_foreign,
                    s.settlement_date,s.foreign_amount
                FROM fx_transactions t
                LEFT JOIN fx_settlements s ON s.transaction_id=t.id
                WHERE t.transaction_date<=? ORDER BY t.id,s.settlement_date""",
                (end,)).fetchall() if _table_exists_financial(conn, "fx_settlements") else _execute(conn, 
                    "SELECT id,original_foreign,NULL settlement_date,NULL foreign_amount FROM fx_transactions WHERE transaction_date<=?",
                    (end,)).fetchall()
            grouped_fx = {}
            for fx_row in settlement_rows:
                fx_id = int(fx_row["id"])
                item = grouped_fx.setdefault(fx_id, {
                    "original": Decimal(str(fx_row["original_foreign"] or "0")),
                    "settled": Decimal("0"),
                })
                if fx_row["settlement_date"] and fx_row["settlement_date"] <= end:
                    item["settled"] += Decimal(str(fx_row["foreign_amount"] or "0"))
            missing_fx_revaluations = []
            for fx_id, balances in grouped_fx.items():
                if balances["original"] - balances["settled"] <= 0:
                    continue
                has_year_end_revaluation = _execute(conn, """SELECT 1 FROM fx_revaluations r
                    JOIN journal_entries je ON je.id=r.journal_entry_id
                    WHERE r.transaction_id=? AND r.revaluation_date=?
                      AND je.status='posted' LIMIT 1""",
                    (fx_id, end)).fetchone()
                if not has_year_end_revaluation:
                    missing_fx_revaluations.append(fx_id)
            add("fx_year_end_revaluation", "إعادة تقييم معاملات الذمم بالعملات الأجنبية",
                "warning" if missing_fx_revaluations else "success",
                len(missing_fx_revaluations),
                (f"عدد معاملات العملات المفتوحة في تاريخ الإقفال دون إعادة تقييم بتاريخ {end}: "
                 f"{len(missing_fx_revaluations)}. يشمل الفحص معاملات مركز العملات للذمم المدينة والدائنة فقط؛ "
                 "راجع أرصدة البنوك والصناديق الأجنبية المسجلة خارج هذا المركز يدويًا.")
                if missing_fx_revaluations else
                f"لا توجد معاملات ذمم مفتوحة في {end} دون إعادة تقييم مسجلة. "
                "راجع أرصدة البنوك والصناديق الأجنبية المسجلة خارج مركز العملات يدويًا.",
                False)
        except (*DB_ERROR, KeyError, TypeError, ValueError, InvalidOperation):
            add("fx_year_end_revaluation", "فحص إعادة تقييم العملات الأجنبية", "warning", 1,
                "تعذر استكمال فحص معاملات العملات؛ راجع مركز العملات يدويًا قبل الإقفال.", False)

    if _table_exists_financial(conn, "period_adjustments") and _table_exists_financial(conn, "period_adjustment_posts"):
        try:
            from period_adjustments import preview_adjustment
            schedules = _execute(conn, """SELECT id FROM period_adjustments
                WHERE status='active' AND start_date<=? AND end_date>=? ORDER BY id""",
                (end, start)).fetchall()
            pending_adjustment_periods = []
            for schedule in schedules:
                preview = preview_adjustment(conn, schedule["id"])
                for period in preview["periods"]:
                    if start <= period["posting_date"] <= end and period["status"] != "posted":
                        pending_adjustment_periods.append(
                            (preview["adjustment"]["adjustment_no"], period["period_start"],
                             period["period_end"], period["amount"], period["status"])
                        )
            details = "؛ ".join(
                f"{number} ({period_start} إلى {period_end}) بمبلغ {amount}"
                for number, period_start, period_end, amount, _status in pending_adjustment_periods[:8]
            )
            if len(pending_adjustment_periods) > 8:
                details += f"؛ وفترات أخرى عددها {len(pending_adjustment_periods) - 8}"
            add("period_adjustments", "تسويات المقدم والمستحق والإيرادات والمخصصات",
                "danger" if pending_adjustment_periods else "success",
                len(pending_adjustment_periods),
                details if pending_adjustment_periods else
                "لا توجد فترات تسوية معلقة داخل السنة المالية.",
                bool(pending_adjustment_periods))
        except (*DB_ERROR, KeyError, TypeError, ValueError):
            add("period_adjustments", "فحص التسويات الدورية", "danger", 1,
                "تعذر التحقق من جداول التسويات؛ لا يمكن تأكيد جاهزية الإقفال قبل مراجعتها.",
                True)

    try:
        snapshot = reconciliation_snapshot(conn)
        unlinked = sum(int(item.get("count", 0) or 0)
                       for item in snapshot.get("unlinked_documents", []))
        add("unlinked_documents", "مستندات تشغيلية غير مرتبطة بقيود",
            "warning" if unlinked else "success", unlinked,
            "مراجعة عامة على مستوى النظام: راجع المستندات التي لم ترتبط بقيود محاسبية.", False)
        pending = int(snapshot.get("pending_stock", 0) or 0)
        add("pending_stock", "حركات مخزون تحتاج معالجة محاسبية",
            "warning" if pending else "success", pending,
            "مراجعة عامة على مستوى النظام: راجع حركات المخزون المعلقة قبل اعتماد الأرصدة النهائية.", False)
        grni = Decimal(str(snapshot.get("grni", "0") or "0"))
        add("grni_balance", "استلامات بضاعة غير مفوترة (GRNI)",
            "warning" if grni else "success", 1 if grni else 0,
            f"رصيد GRNI غير المسوّى على مستوى النظام: {grni}. راجع الاستلامات والفواتير المرتبطة.", False)
        open_ap = Decimal(str(snapshot.get("open_ap", "0") or "0"))
        add("open_payables", "أرصدة موردين مفتوحة",
            "warning" if open_ap else "success", 1 if open_ap else 0,
            f"إجمالي أرصدة الموردين المفتوحة على مستوى النظام: {open_ap}. طابقها مع كشف الموردين.", False)
        difference = snapshot.get("stock_difference")
        has_difference = difference is not None and Decimal(str(difference)) != 0
        add("stock_reconciliation", "فرق المخزون بين الدفاتر والتقييم",
            "warning" if has_difference else "success", 1 if has_difference else 0,
            f"فرق المخزون على مستوى النظام: {difference}." if has_difference else
            "لا يوجد فرق ظاهر بين قيمة المخزون والتقييم.", False)
    except (*DB_ERROR, KeyError, TypeError, ValueError):
        add("operational_checks", "فحوصات الربط التشغيلي والمخزون", "warning", 1,
            "تعذر استكمال بعض فحوصات الربط أو المخزون؛ راجعها يدويًا.", False)

    blockers = [item for item in checks if item["blocking"]]
    warnings = [item for item in checks if item["severity"] == "warning"]
    return {"year": year, "checks": checks, "blockers": blockers,
            "warnings": warnings, "can_close": not blockers,
            "requires_approval": bool(warnings)}


def close_financial_year(conn, year_id, created_by=None, allow_warnings=False):
    """Close P&L balances into retained earnings and lock the financial year."""
    _start_financial_year_transaction(conn)
    try:
        year = _financial_year_or_error(conn, year_id)
        if year["status"] != "open":
            raise ValueError("السنة المالية ليست مفتوحة أو تم إقفالها بالفعل.")
        if year["closing_entry_id"]:
            raise ValueError("يوجد قيد إقفال مرتبط بهذه السنة بالفعل.")
        preclose = build_financial_year_preclose_report(conn, year_id)
        if preclose["blockers"]:
            first = preclose["blockers"][0]
            raise ValueError(f"فشل فحص ما قبل الإقفال: {first['title']} — {first['count']}. {first['details']}")
        if preclose["warnings"] and not allow_warnings:
            raise ValueError("توجد تحذيرات قبل الإقفال. راجع تقرير ما قبل الإقفال، ولا تعتمد الاستثناء إلا بصلاحية المدير.")
        drafts = _execute(conn, """SELECT COUNT(*) FROM journal_entries
            WHERE status='draft' AND entry_date>=? AND entry_date<=?""",
            (year["start_date"], year["end_date"])).fetchone()[0]
        if drafts:
            raise ValueError(f"لا يمكن الإقفال؛ توجد {drafts} قيود مسودة داخل السنة المالية.")
        if _table_exists_financial(conn, "accounting_periods"):
            locked = _execute(conn, """SELECT period_name FROM accounting_periods
                WHERE locked=1 AND start_date<=? AND end_date>=? LIMIT 1""",
                (year["end_date"], year["end_date"])).fetchone()
            if locked:
                raise ValueError(f"لا يمكن الإقفال؛ الفترة المحاسبية {locked['period_name']} مغلقة بالفعل.")
        rows = _execute(conn, """SELECT a.id,a.code,a.name_ar,a.account_type,a.is_group,a.active,
                jl.debit,jl.credit
            FROM journal_lines jl
            JOIN journal_entries je ON je.id=jl.entry_id
            JOIN accounts a ON a.id=jl.account_id
            WHERE je.status='posted' AND je.entry_date>=? AND je.entry_date<=?
              AND a.account_type IN ('revenue','expense')
            ORDER BY a.code,jl.id""", (year["start_date"], year["end_date"])).fetchall()
        balances = {}
        account_meta = {}
        for row in rows:
            account_id = row["id"]
            account_meta[account_id] = row
            debit, credit = Decimal(str(row["debit"] or "0")), Decimal(str(row["credit"] or "0"))
            item = balances.setdefault(account_id, {"debit": Decimal("0"), "credit": Decimal("0")})
            item["debit"] += debit
            item["credit"] += credit
        closing_lines = []
        revenue_total = Decimal("0")
        expense_total = Decimal("0")
        for account_id, amounts in balances.items():
            account = account_meta[account_id]
            if account["is_group"] or not account["active"]:
                raise ValueError(f"حساب قائمة الدخل {account['code']} غير تفصيلي أو غير نشط؛ لا يمكن إقفاله.")
            if account["account_type"] == "revenue":
                natural_balance = amounts["credit"] - amounts["debit"]
                revenue_total += natural_balance
                if natural_balance > 0:
                    closing_lines.append((account_id, natural_balance, Decimal("0"), "إقفال إيرادات السنة"))
                elif natural_balance < 0:
                    closing_lines.append((account_id, Decimal("0"), -natural_balance, "إقفال رصيد إيراد مدين"))
            else:
                natural_balance = amounts["debit"] - amounts["credit"]
                expense_total += natural_balance
                if natural_balance > 0:
                    closing_lines.append((account_id, Decimal("0"), natural_balance, "إقفال مصروفات السنة"))
                elif natural_balance < 0:
                    closing_lines.append((account_id, -natural_balance, Decimal("0"), "إقفال رصيد مصروف دائن"))
        if not closing_lines:
            raise ValueError("لا توجد أرصدة إيرادات أو مصروفات مرحلة داخل السنة لإقفالها.")
        net_profit = revenue_total - expense_total
        if net_profit:
            retained = _retained_earnings_account(conn)
            if net_profit > 0:
                closing_lines.append((retained["id"], Decimal("0"), net_profit, "صافي ربح السنة إلى الأرباح المرحلة"))
            else:
                closing_lines.append((retained["id"], -net_profit, Decimal("0"), "صافي خسارة السنة إلى الأرباح المرحلة"))
        sequence = _history_sequence(conn, year_id, "closing", "created")
        reference = f"FY-{year_id}-CLOSE-{sequence}"
        entry_id = post_journal(
            conn, year["end_date"], reference,
            f"قيد إقفال الإيرادات والمصروفات للسنة المالية {year['year_name']}",
            closing_lines, created_by,
        )
        _execute(conn, "UPDATE journal_entries SET entry_type='year_end_closing' WHERE id=?", (entry_id,))
        _execute(conn, "UPDATE financial_years SET closing_entry_id=?,status='closed' WHERE id=?",
                     (entry_id, year_id))
        _execute(conn, """INSERT INTO financial_year_journal_history
            (financial_year_id,entry_kind,action,entry_id,created_by)
            VALUES(?,'closing','created',?,?)""", (year_id, entry_id, created_by))
        if preclose["warnings"] and allow_warnings and _table_exists_financial(conn, "audit_log"):
            _execute(conn, """INSERT INTO audit_log(user_id,action,details,created_at)
                VALUES(?,?,?,CURRENT_TIMESTAMP)""",
                (created_by, "financial_year_warnings_approved",
                 f"اعتماد تحذيرات الإقفال للسنة {year_id} — قيد الإقفال {entry_id}"))
        conn.commit()
        return entry_id
    except Exception:
        conn.rollback()
        raise


def _table_exists_financial(conn, table_name):
    return table_exists(conn, table_name)


def reverse_financial_year_entry(conn, year_id, entry_kind, reversal_date, reason, created_by=None):
    """Reverse an opening or closing journal without editing the original entry."""
    from datetime import date
    if entry_kind not in {"opening", "closing"}:
        raise ValueError("نوع القيد المطلوب عكسه غير صحيح.")
    try:
        normalized_date = date.fromisoformat(str(reversal_date)).isoformat()
    except (TypeError, ValueError):
        raise ValueError("تاريخ القيد العكسي غير صحيح.")
    if not str(reason or "").strip() or len(str(reason).strip()) > 500:
        raise ValueError("اكتب سببًا للعكس لا يزيد عن 500 حرف.")
    _start_financial_year_transaction(conn)
    try:
        year = _financial_year_or_error(conn, year_id)
        if not (year["start_date"] <= normalized_date <= year["end_date"]):
            raise ValueError("يجب أن يكون تاريخ عكس القيد داخل السنة المالية نفسها.")
        original_id = year["opening_entry_id"] if entry_kind == "opening" else year["closing_entry_id"]
        if not original_id:
            raise ValueError("لا يوجد قيد حالي مرتبط بهذه السنة لعكسه.")
        if entry_kind == "opening":
            if year["status"] != "open" or year["closing_entry_id"]:
                raise ValueError("يجب عكس قيد الإقفال أولًا قبل عكس قيد الافتتاح.")
        else:
            if year["status"] != "closed":
                raise ValueError("عكس قيد الإقفال متاح فقط بعد إقفال السنة.")
            drafts = _execute(conn, """SELECT COUNT(*) FROM journal_entries
                WHERE status='draft' AND entry_date>=? AND entry_date<=?""",
                (year["start_date"], year["end_date"])).fetchone()[0]
            if drafts:
                raise ValueError("لا يمكن عكس الإقفال قبل معالجة القيود المسودة داخل السنة.")
            later_year = _execute(conn, """SELECT fy.year_name FROM financial_years fy
                WHERE fy.start_date>?
                  AND (
                    fy.opening_entry_id IS NOT NULL OR fy.closing_entry_id IS NOT NULL
                    OR EXISTS (SELECT 1 FROM journal_entries je
                        WHERE je.entry_date>=fy.start_date AND je.entry_date<=fy.end_date
                          AND je.status IN ('draft','posted'))
                  )
                ORDER BY fy.start_date,fy.id LIMIT 1""", (year["end_date"],)).fetchone()
            if later_year:
                raise ValueError(
                    f"لا يمكن عكس إقفال السنة؛ السنة التالية ({later_year['year_name']}) "
                    "لها قيد افتتاح أو قيود مسجلة. يجب معالجة السنة التالية أولًا."
                )
            later_entries = _execute(conn, """SELECT id,entry_date FROM journal_entries
                WHERE entry_date>? AND status IN ('draft','posted')
                ORDER BY entry_date,id LIMIT 1""", (year["end_date"],)).fetchone()
            if later_entries:
                raise ValueError(
                    f"لا يمكن عكس إقفال السنة؛ يوجد قيد رقم {later_entries['id']} "
                    f"بتاريخ {later_entries['entry_date']} بعد نهاية السنة. "
                    "يجب معالجة القيود اللاحقة أولًا."
                )
            _execute(conn, "UPDATE financial_years SET status='open' WHERE id=?", (year_id,))
        original = _execute(conn, "SELECT id,description,status FROM journal_entries WHERE id=?",
                                 (original_id,)).fetchone()
        if not original or original["status"] != "posted":
            raise ValueError("القيد الأصلي غير موجود أو غير مرحّل.")
        original_lines = _execute(conn, """SELECT account_id,debit,credit,memo
            FROM journal_lines WHERE entry_id=? ORDER BY id""", (original_id,)).fetchall()
        if len(original_lines) < 2:
            raise ValueError("القيد الأصلي لا يحتوي على سطور كافية للعكس.")
        reversal_lines = [
            (row["account_id"], row["credit"], row["debit"],
             f"عكس قيد {entry_kind}: {row['memo'] or ''}")
            for row in original_lines
        ]
        sequence = _history_sequence(conn, year_id, entry_kind, "reversed")
        reference = f"FY-{year_id}-{entry_kind.upper()}-REV-{sequence}"
        reversal_id = post_journal(
            conn, normalized_date, reference,
            f"قيد عكسي للقيد رقم {original_id} — {str(reason).strip()}",
            reversal_lines, created_by,
        )
        if entry_kind == "closing":
            _execute(conn, "UPDATE journal_entries SET entry_type='year_end_closing_reversal' WHERE id=?", (reversal_id,))
        else:
            _execute(conn, "UPDATE journal_entries SET entry_type='opening_reversal' WHERE id=?", (reversal_id,))
        if entry_kind == "closing":
            _execute(conn, "UPDATE financial_years SET closing_entry_id=NULL,status='open' WHERE id=?", (year_id,))
        else:
            _execute(conn, "UPDATE financial_years SET opening_entry_id=NULL WHERE id=?", (year_id,))
        _execute(conn, """INSERT INTO financial_year_journal_history
            (financial_year_id,entry_kind,action,entry_id,related_entry_id,created_by)
            VALUES(?,?,'reversed',?,?,?)""",
            (year_id, entry_kind, reversal_id, original_id, created_by))
        conn.commit()
        return reversal_id
    except Exception:
        conn.rollback()
        raise



def _require(permission):
    def decorate(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            if not session.get("user_id"):
                return redirect(url_for("login"))
            allowed = set(session.get("permissions", ()))
            # Existing installations may have saved permission JSON before
            # cash-flow reporting was introduced. Treat report-view access as
            # sufficient to discover and open this financial report.
            if permission not in allowed and not (
                permission == "cashflow.view" and "reports.view" in allowed
            ):
                abort(403)
            return fn(*args, **kwargs)
        return wrapped
    return decorate


def _connect():
    """Open the configured application database using the shared connection policy."""
    return connect_database(DB_PATH)


@planning_bp.route("/financial-years", methods=["GET", "POST"])
@_require("reports.view")
def financial_years():
    conn = _connect()
    if request.method == "POST":
        if "journal.manage" not in set(session.get("permissions", ())):
            conn.close()
            abort(403)
        try:
            create_financial_year(
                conn,
                request.form.get("year_name", ""),
                request.form.get("start_date", ""),
                request.form.get("end_date", ""),
                session.get("user_id"),
            )
            flash("تم إنشاء السنة المالية بنجاح. لم يتم إنشاء أي قيود محاسبية.", "success")
            return redirect(url_for("planning.financial_years"))
        except (ValueError, *DB_INTEGRITY_ERROR) as exc:
            flash(str(exc) or "تعذر إنشاء السنة المالية؛ تحقق من الاسم والتواريخ.", "error")
        finally:
            conn.close()
        return redirect(url_for("planning.financial_years"))
    years = list_financial_years(conn)
    history = _execute(conn, """SELECT h.id,h.entry_kind,h.action,h.entry_id,
            h.related_entry_id,h.created_at,fy.year_name,je.entry_date,je.reference
        FROM financial_year_journal_history h
        JOIN financial_years fy ON fy.id=h.financial_year_id
        JOIN journal_entries je ON je.id=h.entry_id
        ORDER BY h.id DESC LIMIT 100""").fetchall()
    conn.close()
    return render_template("financial_years.html", years=years, history=history)


@planning_bp.route("/financial-years/<int:year_id>/pre-close")
@_require("reports.view")
def financial_year_preclose(year_id):
    conn = _connect()
    try:
        report = build_financial_year_preclose_report(conn, year_id)
        return render_template("financial_year_preclose.html", report=report,
            can_approve=("journal.manage" in set(session.get("permissions", ()))
                         and session.get("role") == "admin"))
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("planning.financial_years"))
    finally:
        conn.close()


@planning_bp.route("/financial-years/<int:year_id>/opening", methods=["GET", "POST"])
@_require("journal.manage")
def financial_year_opening(year_id):
    conn = _connect()
    try:
        year = _financial_year_or_error(conn, year_id)
        if year["status"] != "open":
            flash("لا يمكن إدخال أرصدة افتتاح لسنة غير مفتوحة.", "error")
            return redirect(url_for("planning.financial_years"))
        if year["opening_entry_id"]:
            flash("قيد الافتتاح موجود بالفعل؛ استخدم إجراء العكس لتصحيحه.", "warning")
            return redirect(url_for("planning.financial_years"))
        accounts = _execute(conn, """SELECT id,code,name_ar,account_type FROM accounts
            WHERE active=1 AND is_group=0 AND account_type IN ('asset','liability','equity')
            ORDER BY CASE account_type WHEN 'asset' THEN 1 WHEN 'liability' THEN 2 ELSE 3 END,code""").fetchall()
        if request.method == "POST":
            balances = {str(account["id"]): request.form.get("balance_" + str(account["id"]), "0")
                        for account in accounts}
            entry_id = create_opening_entry(conn, year_id, balances, session.get("user_id"))
            flash(f"تم ترحيل قيد الافتتاح رقم {entry_id} بتاريخ {year['start_date']}.", "success")
            return redirect(url_for("planning.financial_years"))
        return render_template("financial_year_opening.html", year=year, accounts=accounts)
    except (ValueError, *DB_INTEGRITY_ERROR) as exc:
        flash(str(exc) or "تعذر تنفيذ قيد الافتتاح.", "error")
        return redirect(url_for("planning.financial_years"))
    finally:
        conn.close()


@planning_bp.route("/financial-years/<int:year_id>/close", methods=["POST"])
@_require("journal.manage")
def financial_year_close(year_id):
    conn = _connect()
    try:
        allow_warnings = request.form.get("approve_warnings") == "1"
        if allow_warnings and session.get("role") != "admin":
            abort(403)
        entry_id = close_financial_year(conn, year_id, session.get("user_id"),
                                        allow_warnings=allow_warnings)
        flash(f"تم إقفال السنة المالية بقيد رقم {entry_id}.", "success")
    except (ValueError, *DB_INTEGRITY_ERROR) as exc:
        flash(str(exc) or "تعذر إقفال السنة المالية.", "error")
    finally:
        conn.close()
    return redirect(url_for("planning.financial_years"))


@planning_bp.route("/financial-years/<int:year_id>/reverse/<entry_kind>", methods=["POST"])
@_require("journal.manage")
def financial_year_reverse(year_id, entry_kind):
    if entry_kind == "closing" and session.get("role") != "admin":
        abort(403)
    conn = _connect()
    try:
        reversal_id = reverse_financial_year_entry(
            conn, year_id, entry_kind,
            request.form.get("reversal_date", ""),
            request.form.get("reason", ""),
            session.get("user_id"),
        )
        flash(f"تم إنشاء القيد العكسي رقم {reversal_id}، مع الاحتفاظ بالقيد الأصلي.", "success")
    except (ValueError, *DB_INTEGRITY_ERROR) as exc:
        flash(str(exc) or "تعذر عكس القيد.", "error")
    finally:
        conn.close()
    return redirect(url_for("planning.financial_years"))


def build_budget_comparison(conn, budget_id):
    """Compare posted ledger activity with a budget, scoped to its optional dimensions."""
    plan = _execute(conn, "SELECT * FROM budget_plans WHERE id=?", (budget_id,)).fetchone()
    if not plan:
        raise ValueError("الموازنة غير موجودة.")
    dimensions = []
    params = [plan["date_from"], plan["date_to"]]
    for key in ("branch_id", "cost_center_id", "project_id"):
        if plan[key] is not None:
            dimensions.append(f"jl.{key}=?")
            params.append(plan[key])
    dimension_sql = (" AND " + " AND ".join(dimensions)) if dimensions else ""
    rows = _execute(conn, f"""
      SELECT a.id,a.code,a.name_ar,a.account_type,b.amount,
        (SELECT {decimal_net_expression(conn, "CASE WHEN a.account_type='revenue' THEN jl.credit ELSE jl.debit END", "CASE WHEN a.account_type='revenue' THEN jl.debit ELSE jl.credit END")}
          FROM journal_lines jl JOIN journal_entries je ON je.id=jl.entry_id
          WHERE jl.account_id=a.id AND je.status='posted'
            AND je.entry_date>=? AND je.entry_date<=? {dimension_sql}) actual
      FROM budget_lines b JOIN accounts a ON a.id=b.account_id
      WHERE b.budget_id=? ORDER BY a.account_type,a.code
    """, (*params, budget_id)).fetchall()
    results = []
    for row in rows:
        budget = _money(row["amount"])
        actual = Decimal(str(row["actual"] or "0"))
        variance = actual - budget
        variance_pct = (variance * 100 / budget) if budget else None
        utilization_pct = (actual * 100 / budget) if budget else None
        if row["account_type"] == "expense":
            status = "over" if actual > budget else ("warning" if budget and actual >= budget * Decimal("0.80") else "ok")
        else:
            status = "behind" if budget and actual < budget * Decimal("0.80") else ("ahead" if actual > budget else "ok")
        results.append({**dict(row), "budget": budget, "actual": actual,
                        "variance": variance, "variance_pct": variance_pct,
                        "utilization_pct": utilization_pct, "status": status})
    totals = {}
    for kind in ("revenue", "expense"):
        totals[kind + "_budget"] = sum((x["budget"] for x in results if x["account_type"] == kind), Decimal("0"))
        totals[kind + "_actual"] = sum((x["actual"] for x in results if x["account_type"] == kind), Decimal("0"))
    return plan, results, totals


def build_budget_monthly_comparison(conn, budget_id):
    plan = _execute(conn, "SELECT * FROM budget_plans WHERE id=?", (budget_id,)).fetchone()
    if not plan:
        raise ValueError("الموازنة غير موجودة.")
    dimensions = []
    params = [plan["date_from"], plan["date_to"]]
    for key in ("branch_id", "cost_center_id", "project_id"):
        if plan[key] is not None:
            dimensions.append(f"jl.{key}=?")
            params.append(plan[key])
    dimension_sql = (" AND " + " AND ".join(dimensions)) if dimensions else ""
    month_expr = month_key_expression(conn, "je.entry_date")
    actual_rows = _execute(conn, f"""
      SELECT jl.account_id,{month_expr} period,
        {decimal_net_expression(conn, "CASE WHEN a.account_type='revenue' THEN jl.credit ELSE jl.debit END", "CASE WHEN a.account_type='revenue' THEN jl.debit ELSE jl.credit END")} actual
      FROM journal_lines jl JOIN journal_entries je ON je.id=jl.entry_id
      JOIN accounts a ON a.id=jl.account_id
      WHERE je.status='posted' AND je.entry_date>=? AND je.entry_date<=? {dimension_sql}
      GROUP BY jl.account_id,{month_expr}
    """, params).fetchall()
    actuals = {(r["account_id"], r["period"]): Decimal(str(r["actual"] or "0")) for r in actual_rows}
    planned = _execute(conn, """
      SELECT ml.account_id,ml.period,ml.amount,a.code,a.name_ar,a.account_type
      FROM budget_monthly_lines ml JOIN accounts a ON a.id=ml.account_id
      WHERE ml.budget_id=? ORDER BY ml.period,a.account_type,a.code
    """, (budget_id,)).fetchall()
    results = []
    for row in planned:
        budget = _money(row["amount"])
        actual = actuals.get((row["account_id"], row["period"]), Decimal("0"))
        variance = actual - budget
        utilization = actual * 100 / budget if budget else None
        if row["account_type"] == "expense":
            status = "over" if actual > budget else ("warning" if budget and actual >= budget * Decimal("0.80") else "ok")
        else:
            status = "behind" if budget and actual < budget * Decimal("0.80") else ("ahead" if actual > budget else "ok")
        results.append({**dict(row), "budget": budget, "actual": actual, "variance": variance,
                        "utilization_pct": utilization, "status": status})
    return plan, results


@planning_bp.route("/budget", methods=["GET", "POST"])
@_require("budget.view")
def budget_index():
    conn = _connect()
    if request.method == "POST":
        if "budget.manage" not in set(session.get("permissions", ())):
            conn.close()
            abort(403)
        try:
            name = request.form.get("name", "").strip()
            start = request.form.get("date_from", "")
            end = request.form.get("date_to", "")
            from datetime import date
            date.fromisoformat(start); date.fromisoformat(end)
            if not name or len(name) > 120 or end < start:
                raise ValueError("أدخل اسمًا وفترة موازنة صحيحة.")
            dimensions = {}
            for key, table in (("branch_id", "branches"), ("cost_center_id", "cost_centers"), ("project_id", "projects")):
                raw = request.form.get(key, "").strip()
                dimensions[key] = int(raw) if raw else None
                if dimensions[key] is not None:
                    valid = _execute(conn, f"SELECT 1 FROM {table} WHERE id=? AND active=1", (dimensions[key],)).fetchone()
                    if not valid:
                        raise ValueError("أحد الأبعاد التحليلية المحددة غير موجود أو غير نشط.")
            ids = request.form.getlist("account_id")
            if not ids:
                raise ValueError("أضف حسابًا واحدًا على الأقل وحدد موازنته.")
            lines = []
            for account_id in ids:
                raw_amount = request.form.get("amount_" + str(account_id), "0")
                account = _execute(conn, "SELECT id,account_type,is_group,active FROM accounts WHERE id=?", (int(account_id),)).fetchone()
                if not account or account["is_group"] or not account["active"] or account["account_type"] not in {"revenue","expense"}:
                    raise ValueError("الموازنة تقبل حسابات الإيرادات والمصروفات التفصيلية النشطة فقط.")
                lines.append((int(account_id), str(_money(raw_amount))))
            if len({x[0] for x in lines}) != len(lines):
                raise ValueError("لا تكرر الحساب داخل الموازنة نفسها.")
            begin_write_transaction(conn)
            budget_id = insert_and_get_id(conn, """INSERT INTO budget_plans
                (name,date_from,date_to,created_by,branch_id,cost_center_id,project_id,status)
                VALUES(?,?,?,?,?,?,?,'draft')""",
                (name,start,end,session["user_id"],dimensions["branch_id"],
                 dimensions["cost_center_id"],dimensions["project_id"]))
            _executemany(conn, "INSERT INTO budget_lines(budget_id,account_id,amount) VALUES(?,?,?)",
                             [(budget_id, aid, amount) for aid,amount in lines])
            months = _budget_months(start, end)
            monthly_values = []
            for account_id, amount in lines:
                monthly_values.extend((budget_id, account_id, month, str(value))
                    for month, value in zip(months, _split_monthly_amount(_money(amount), months)))
            _executemany(conn, """INSERT INTO budget_monthly_lines
                (budget_id,account_id,period,amount) VALUES(?,?,?,?)""", monthly_values)
            _budget_audit(conn, budget_id, "created", session["user_id"], "إنشاء الموازنة وتوزيعها مبدئيًا بالتساوي على الأشهر")
            conn.commit()
            flash("تم حفظ الموازنة كمسودة. راجع التوزيع الشهري ثم أرسلها للاعتماد.", "success")
            return redirect(url_for("planning.budget_detail", budget_id=budget_id))
        except (ValueError, TypeError, InvalidOperation, *DB_INTEGRITY_ERROR) as exc:
            conn.rollback()
            flash(str(exc) or "تعذر حفظ الموازنة.", "error")
    plans = _execute(conn, """SELECT bp.*,
        COALESCE(br.name_ar,'كل الفروع') branch_name,
        COALESCE(cc.name_ar,'كل مراكز التكلفة') cost_center_name,
        COALESCE(p.name_ar,'كل المشروعات') project_name
        FROM budget_plans bp LEFT JOIN branches br ON br.id=bp.branch_id
        LEFT JOIN cost_centers cc ON cc.id=bp.cost_center_id
        LEFT JOIN projects p ON p.id=bp.project_id
        ORDER BY bp.date_from DESC,bp.id DESC""").fetchall()
    accounts = _execute(conn, "SELECT id,code,name_ar,account_type FROM accounts WHERE active=1 AND is_group=0 AND account_type IN ('revenue','expense') ORDER BY account_type,code").fetchall()
    dimension_options = {
        "branches": _execute(conn, "SELECT id,code,name_ar FROM branches WHERE active=1 ORDER BY code").fetchall(),
        "cost_centers": _execute(conn, "SELECT id,code,name_ar FROM cost_centers WHERE active=1 ORDER BY code").fetchall(),
        "projects": _execute(conn, "SELECT id,code,name_ar FROM projects WHERE active=1 ORDER BY code").fetchall(),
    }
    conn.close()
    return render_template("budget.html", plans=plans, accounts=accounts, dimension_options=dimension_options)


@planning_bp.route("/budget/<int:budget_id>")
@_require("budget.view")
def budget_detail(budget_id):
    conn = _connect()
    try:
        plan, results, totals = build_budget_comparison(conn, budget_id)
        plan, monthly_rows = build_budget_monthly_comparison(conn, budget_id)
        months = _budget_months(plan["date_from"], plan["date_to"])
        monthly_matrix = {}
        for row in _execute(conn, "SELECT account_id,period,amount FROM budget_monthly_lines WHERE budget_id=?", (budget_id,)):
            monthly_matrix.setdefault(row["account_id"], {})[row["period"]] = row["amount"]
        history = _execute(conn, """SELECT l.*,u.username FROM budget_audit_log l
            LEFT JOIN users u ON u.id=l.actor_id WHERE l.budget_id=? ORDER BY l.id DESC""", (budget_id,)).fetchall()
    except ValueError:
        conn.close()
        abort(404)
    conn.close()
    return render_template("budget_detail.html", plan=plan, rows=results, months=months,
        monthly_rows=monthly_rows, monthly_matrix=monthly_matrix, history=history, **totals)


@planning_bp.post("/budget/<int:budget_id>/distribution")
@_require("budget.manage")
def budget_update_distribution(budget_id):
    conn = _connect()
    try:
        begin_write_transaction(conn)
        plan = _execute(conn, "SELECT * FROM budget_plans WHERE id=?", (budget_id,)).fetchone()
        if not plan:
            abort(404)
        if plan["status"] not in {"draft", "rejected"}:
            abort(409, "لا يمكن تعديل توزيع موازنة مرسلة أو معتمدة.")
        months = _budget_months(plan["date_from"], plan["date_to"])
        lines = _execute(conn, "SELECT account_id,amount FROM budget_lines WHERE budget_id=?", (budget_id,)).fetchall()
        updates = []
        revised_totals = []
        for line in lines:
            values = []
            for month in months:
                value = _money(request.form.get(f"monthly_amount_{line['account_id']}_{month}", ""))
                values.append(value)
                updates.append((str(value), budget_id, line["account_id"], month))
            revised_totals.append((str(sum(values, Decimal("0"))), budget_id, line["account_id"]))
        _executemany(conn, """UPDATE budget_monthly_lines SET amount=?
            WHERE budget_id=? AND account_id=? AND period=?""", updates)
        _executemany(conn, "UPDATE budget_lines SET amount=? WHERE budget_id=? AND account_id=?", revised_totals)
        _budget_audit(conn, budget_id, "distribution_updated", session["user_id"],
            "حفظ التوزيع الشهري وإعادة احتساب إجمالي الموازنة لكل حساب من مجموع الأشهر")
        conn.commit()
        flash("تم حفظ التوزيع الشهري.", "success")
    except (ValueError, InvalidOperation, TypeError) as exc:
        conn.rollback()
        flash(str(exc), "error")
    finally:
        conn.close()
    return redirect(url_for("planning.budget_detail", budget_id=budget_id))


@planning_bp.post("/budget/<int:budget_id>/submit")
@_require("budget.manage")
def budget_submit(budget_id):
    conn = _connect()
    try:
        begin_write_transaction(conn)
        plan = _execute(conn, "SELECT * FROM budget_plans WHERE id=?", (budget_id,)).fetchone()
        if not plan:
            abort(404)
        if plan["status"] not in {"draft", "rejected"}:
            abort(409)
        mismatch = _execute(conn, f"""SELECT b.account_id,b.amount,{decimal_sum_expression(conn, "ml.amount")} monthly_total
            FROM budget_lines b LEFT JOIN budget_monthly_lines ml
              ON ml.budget_id=b.budget_id AND ml.account_id=b.account_id
            WHERE b.budget_id=? GROUP BY b.account_id,b.amount""", (budget_id,)).fetchall()
        for row in mismatch:
            if _money(row["amount"]) != _money(row["monthly_total"]):
                raise ValueError("لا يمكن إرسال الموازنة قبل تساوي إجمالي التوزيع الشهري مع المبلغ السنوي لكل حساب.")
        _execute(conn, """UPDATE budget_plans SET status='pending',submitted_by=?,submitted_at=CURRENT_TIMESTAMP,
            rejection_reason=NULL WHERE id=?""", (session["user_id"], budget_id))
        _budget_audit(conn, budget_id, "submitted", session["user_id"], "إرسال الموازنة للاعتماد")
        conn.commit()
        flash("تم إرسال الموازنة للاعتماد.", "success")
    except ValueError as exc:
        conn.rollback()
        flash(str(exc), "error")
    finally:
        conn.close()
    return redirect(url_for("planning.budget_detail", budget_id=budget_id))


@planning_bp.post("/budget/<int:budget_id>/approve")
@_require("budget.approve")
def budget_approve(budget_id):
    conn = _connect()
    try:
        begin_write_transaction(conn)
        plan = _execute(conn, "SELECT * FROM budget_plans WHERE id=?", (budget_id,)).fetchone()
        if not plan:
            abort(404)
        if plan["status"] != "pending":
            abort(409)
        if plan["created_by"] == session["user_id"]:
            abort(403, "لا يجوز لمنشئ الموازنة اعتمادها بنفسه.")
        _execute(conn, "UPDATE budget_plans SET status='approved',approved_by=?,approved_at=CURRENT_TIMESTAMP WHERE id=?",
                     (session["user_id"], budget_id))
        _budget_audit(conn, budget_id, "approved", session["user_id"], "اعتماد الموازنة")
        conn.commit()
        flash("تم اعتماد الموازنة. أصبحت للعرض فقط ولا يمكن تعديلها.", "success")
    finally:
        conn.close()
    return redirect(url_for("planning.budget_detail", budget_id=budget_id))


@planning_bp.post("/budget/<int:budget_id>/reject")
@_require("budget.approve")
def budget_reject(budget_id):
    conn = _connect()
    try:
        reason = request.form.get("reason", "").strip()
        if not reason:
            raise ValueError("يجب كتابة سبب رفض الموازنة.")
        begin_write_transaction(conn)
        plan = _execute(conn, "SELECT * FROM budget_plans WHERE id=?", (budget_id,)).fetchone()
        if not plan:
            abort(404)
        if plan["status"] != "pending" or plan["created_by"] == session["user_id"]:
            abort(409)
        _execute(conn, "UPDATE budget_plans SET status='rejected',rejection_reason=? WHERE id=?",
                     (reason[:1000], budget_id))
        _budget_audit(conn, budget_id, "rejected", session["user_id"], reason)
        conn.commit()
        flash("تم رفض الموازنة مع تسجيل السبب.", "success")
    except ValueError as exc:
        conn.rollback()
        flash(str(exc), "error")
    finally:
        conn.close()
    return redirect(url_for("planning.budget_detail", budget_id=budget_id))


@planning_bp.get("/budget/<int:budget_id>/export/<fmt>")
@_require("budget.view")
def budget_export(budget_id, fmt):
    if fmt not in {"excel", "pdf"}:
        abort(404)
    conn = _connect()
    try:
        plan, rows = build_budget_monthly_comparison(conn, budget_id)
        company = _execute(conn, "SELECT setting_value FROM company_settings WHERE setting_key='company_name'").fetchone()
        currency = _execute(conn, "SELECT setting_value FROM company_settings WHERE setting_key='base_currency'").fetchone()
        company_name = company["setting_value"] if company else "NEXUS ERP"
        currency_code = currency["setting_value"] if currency else ""
        if not rows:
            rows = []
        report_rows = [[r["period"], r["code"], r["name_ar"],
                        "إيراد" if r["account_type"] == "revenue" else "مصروف",
                        r["budget"], r["actual"], r["variance"],
                        r["utilization_pct"] if r["utilization_pct"] is not None else "—",
                        r["status"]] for r in rows]
    except ValueError:
        conn.close()
        abort(404)
    finally:
        conn.close()
    headers = ["الشهر", "كود الحساب", "اسم الحساب", "النوع", "المخطط", "الفعلي", "الانحراف", "نسبة الاستهلاك %", "الحالة"]
    if fmt == "excel":
        try:
            from openpyxl import Workbook
            from openpyxl.styles import Font, PatternFill, Alignment
            from openpyxl.utils import get_column_letter
        except ImportError:
            return Response("مكتبة openpyxl غير مثبتة.", status=500, mimetype="text/plain; charset=utf-8")
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Budget vs Actual"
        sheet.append([company_name])
        sheet.append(["مقارنة الموازنة بالفعلي", plan["name"]])
        sheet.append(["الفترة", format_export_dates(plan["date_from"]), format_export_dates(plan["date_to"]), "العملة", currency_code])
        sheet.append(headers)
        for row in report_rows:
            sheet.append([float(v) if isinstance(v, Decimal) else v for v in row])
        for cell in sheet[4]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="167D8D")
            cell.alignment = Alignment(horizontal="center")
        sheet.freeze_panes = "A5"
        sheet.auto_filter.ref = f"A4:I{max(4, sheet.max_row)}"
        for column in sheet.columns:
            letter = get_column_letter(column[0].column)
            sheet.column_dimensions[letter].width = min(34, max(13, max(len(str(c.value or "")) for c in column) + 2))
        output = io.BytesIO()
        workbook.save(output)
        output.seek(0)
        return send_file(output, as_attachment=True,
            download_name=f"budget-actual-{budget_id}.xlsx",
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    try:
        import os
        import arabic_reshaper
        from bidi.algorithm import get_display
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_CENTER, TA_RIGHT
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import mm
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
        from xml.sax.saxutils import escape
    except ImportError:
        return Response("مكتبات PDF غير مثبتة.", status=500, mimetype="text/plain; charset=utf-8")
    font_candidates = [
        os.path.join(os.path.dirname(__file__), "static", "fonts", "DejaVuSans.ttf"),
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed.ttf",
        "C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/tahoma.ttf",
    ]
    font_path = next((path for path in font_candidates if os.path.isfile(path)), None)
    if not font_path:
        return Response("تعذر إنشاء PDF: أضف DejaVuSans.ttf إلى static/fonts.", status=503, mimetype="text/plain; charset=utf-8")
    if "NexusArabic" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("NexusArabic", font_path))
    def pdf_text(value):
        return escape(get_display(arabic_reshaper.reshape("" if value is None else str(value))))
    output = io.BytesIO()
    page_size = landscape(A4)
    doc = SimpleDocTemplate(output, pagesize=page_size, rightMargin=10*mm, leftMargin=10*mm,
                            topMargin=12*mm, bottomMargin=12*mm, title="مقارنة الموازنة بالفعلي", author=company_name)
    styles = getSampleStyleSheet()
    heading = ParagraphStyle("BudgetHeading", parent=styles["Title"], fontName="NexusArabic", fontSize=14, leading=19, alignment=TA_CENTER)
    cell = ParagraphStyle("BudgetCell", parent=styles["BodyText"], fontName="NexusArabic", fontSize=7, leading=9, alignment=TA_RIGHT)
    head = ParagraphStyle("BudgetHead", parent=cell, textColor=colors.white, alignment=TA_CENTER)
    story = [Paragraph(pdf_text(company_name), heading),
             Paragraph(pdf_text("مقارنة الموازنة بالفعلي — " + plan["name"]), heading),
             Paragraph(pdf_text(f"الفترة: {format_export_dates(plan['date_from'])} إلى {format_export_dates(plan['date_to'])} | العملة: {currency_code}"), cell),
             Spacer(1, 4*mm)]
    data = [[Paragraph(pdf_text(x), head) for x in reversed(headers)]]
    for row in report_rows:
        data.append([Paragraph(pdf_text(f"{x:,.2f}" if isinstance(x, Decimal) else x), cell) for x in reversed(row)])
    table = Table(data, colWidths=[(page_size[0]-20*mm)/len(headers)]*len(headers), repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND",(0,0),(-1,0),colors.HexColor("#167D8D")),
        ("GRID",(0,0),(-1,-1),0.3,colors.HexColor("#B7C3CC")),
        ("VALIGN",(0,0),(-1,-1),"MIDDLE"),
        ("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white,colors.HexColor("#F3F6F8")]),
        ("LEFTPADDING",(0,0),(-1,-1),3),("RIGHTPADDING",(0,0),(-1,-1),3),
        ("TOPPADDING",(0,0),(-1,-1),3),("BOTTOMPADDING",(0,0),(-1,-1),3),
    ]))
    story.append(table)
    doc.build(story)
    output.seek(0)
    return send_file(output, as_attachment=True, download_name=f"budget-actual-{budget_id}.pdf", mimetype="application/pdf")


def build_balance_sheet_snapshot(conn, as_of):
    account_columns = table_columns(conn, "accounts")
    section_expr = "a.statement_section" if "statement_section" in account_columns else "NULL"
    subtype_expr = "a.account_subtype" if "account_subtype" in account_columns else "NULL"

    # Exclude only opening entries explicitly classified as copies when
    # they were created. Never infer this from today's ledger contents: a late
    # historical import must not retroactively change prior balance sheets.
    excluded_opening_ids = set()
    unclassified_opening_count = 0
    if _table_exists_financial(conn, "financial_years") and _table_exists_financial(
        conn, "financial_year_journal_history"
    ):
        history_columns = table_columns(conn, "financial_year_journal_history")
        if "ledger_history_copied" in history_columns:
            opening_rows = _execute(conn, """
                SELECT h.entry_id
                FROM financial_year_journal_history h
                JOIN financial_years fy ON fy.id=h.financial_year_id
                WHERE h.entry_kind='opening' AND h.action='created'
                  AND h.ledger_history_copied=1 AND fy.start_date<=?
            """, (as_of,)).fetchall()
            unclassified_opening_count = _execute(conn, """
                SELECT COUNT(*)
                FROM financial_year_journal_history h
                JOIN financial_years fy ON fy.id=h.financial_year_id
                WHERE h.entry_kind='opening' AND h.action='created'
                  AND h.ledger_history_copied IS NULL AND fy.start_date<=?
            """, (as_of,)).fetchone()[0]
            excluded_opening_ids = {int(row["entry_id"]) for row in opening_rows}
            if excluded_opening_ids:
                marks = ",".join("?" for _ in excluded_opening_ids)
                reversal_rows = _execute(conn, f"""
                    SELECT entry_id FROM financial_year_journal_history
                    WHERE entry_kind='opening' AND action='reversed'
                      AND related_entry_id IN ({marks})
                """, tuple(sorted(excluded_opening_ids))).fetchall()
                excluded_opening_ids.update(int(row["entry_id"]) for row in reversal_rows)
        else:
            unclassified_opening_count = _execute(conn, """
                SELECT COUNT(*)
                FROM financial_year_journal_history h
                JOIN financial_years fy ON fy.id=h.financial_year_id
                WHERE h.entry_kind='opening' AND h.action='created' AND fy.start_date<=?
            """, (as_of,)).fetchone()[0]

    exclusion_sql = ""
    exclusion_params = []
    if excluded_opening_ids:
        marks = ",".join("?" for _ in excluded_opening_ids)
        exclusion_sql = f" AND je.id NOT IN ({marks})"
        exclusion_params = sorted(excluded_opening_ids)

    rows = _execute(conn, f"""SELECT a.id,a.code,a.name_ar,a.account_type,a.parent_id,a.is_group,
        {section_expr} statement_section,{subtype_expr} account_subtype,
        {decimal_sum_expression(conn, "CASE WHEN je.id IS NOT NULL THEN jl.debit ELSE '0' END")} debit,
        {decimal_sum_expression(conn, "CASE WHEN je.id IS NOT NULL THEN jl.credit ELSE '0' END")} credit
      FROM accounts a
      LEFT JOIN journal_lines jl ON jl.account_id=a.id
      LEFT JOIN journal_entries je ON je.id=jl.entry_id AND je.status='posted'
        AND je.entry_date<=? {exclusion_sql}
      GROUP BY a.id ORDER BY a.code""", (as_of, *exclusion_params)).fetchall()
    accounts = {}
    for row in rows:
        debit=Decimal(str(row["debit"] or "0"))
        credit=Decimal(str(row["credit"] or "0"))
        kind=row["account_type"]
        balance=debit-credit if kind=="asset" or kind=="expense" else credit-debit
        accounts[row["id"]]={**dict(row),"balance":balance,
            "statement_section": row["statement_section"] or {
                "asset": "assets", "liability": "liabilities",
                "equity": "equity", "revenue": "revenue", "expense": "operating_expenses",
            }.get(kind, "unclassified")}
    totals={"asset":Decimal("0"),"liability":Decimal("0"),"equity":Decimal("0"),
            "revenue":Decimal("0"),"expense":Decimal("0")}
    for item in accounts.values():
        if item["is_group"]:
            continue
        totals[item["account_type"]]+=item["balance"]
    net_profit=totals["revenue"]-totals["expense"]
    group_totals={account_id:Decimal("0") for account_id,item in accounts.items() if item["is_group"]}
    for account_id,item in accounts.items():
        if item["is_group"]:
            continue
        parent=item["parent_id"]
        seen=set()
        while parent and parent in accounts and parent not in seen:
            seen.add(parent)
            if accounts[parent]["is_group"]:
                group_totals[parent]+=item["balance"]
            parent=accounts[parent]["parent_id"]
    children={}
    for item in accounts.values():
        children.setdefault(item["parent_id"],[]).append(item)
    sections={}
    for kind in ("asset","liability","equity"):
        flat=[]
        def walk(parent_id,depth=0,seen=None):
            seen=seen or set()
            for item in sorted(children.get(parent_id,[]),key=lambda x:x["code"]):
                if item["account_type"]!=kind or item["id"] in seen:
                    continue
                value=group_totals.get(item["id"],Decimal("0")) if item["is_group"] else item["balance"]
                flat.append({**item,"display_balance":value,"depth":depth,
                    "statement_section": item.get("statement_section") or kind})
                walk(item["id"],depth+1,seen|{item["id"]})
        walk(None)
        sections[kind]=flat
    assets=totals["asset"]
    liabilities=totals["liability"]
    equity=totals["equity"]
    total_equity=equity+net_profit
    negative_asset_accounts = [
        {"id": item["id"], "code": item["code"], "name_ar": item["name_ar"],
         "balance": item["balance"]}
        for item in accounts.values()
        if not item["is_group"] and item["account_type"] == "asset" and item["balance"] < 0
        and item.get("account_subtype") not in {
            "accumulated_depreciation_or_amortization", "impairment_allowance"
        }
    ]
    negative_asset_accounts.sort(key=lambda item: item["balance"])
    return {"sections":sections,"total_assets":assets,"total_liabilities":liabilities,
            "equity_before_profit":equity,"net_profit":net_profit,"total_equity":total_equity,
            "negative_asset_accounts":negative_asset_accounts,
            "balance_difference":assets-liabilities-total_equity,
            "balanced":assets==liabilities+total_equity,"as_of":as_of,
            "opening_balance_unclassified_count": int(unclassified_opening_count),
            "opening_balance_warning": (
                "توجد قيود افتتاحية تاريخية لم يُحفظ معها تصنيف يثبت هل هي أرصدة منقولة أم أرصدة أولية. "
                "لم يستبعدها النظام تلقائيًا؛ راجعها قبل اعتماد المركز المالي."
                if unclassified_opening_count else ""
            )}


@planning_bp.route("/reports/balance-sheet")
@_require("reports.view")
def balance_sheet():
    from datetime import date
    raw=request.args.get("as_of","").strip()
    try:
        as_of=date.fromisoformat(raw) if raw else date.today()
    except ValueError:
        flash("تاريخ قائمة المركز المالي غير صحيح.","error")
        as_of=date.today()
    conn=_connect()
    try:
        snapshot=build_balance_sheet_snapshot(conn,as_of.isoformat())
        currency_row=_execute(conn, "SELECT setting_value FROM company_settings WHERE setting_key='base_currency'").fetchone()
        base_currency=currency_row["setting_value"] if currency_row else ""
    finally:
        conn.close()
    return render_template("balance_sheet.html",s=snapshot,base_currency=base_currency)


def build_cash_flow_snapshot(conn, start, end):
    """Build an IAS 7-oriented cash-flow snapshot for an inclusive date range."""
    from management_dashboard import load_management_account_roles
    role_mapping = load_management_account_roles(conn)
    cash_ids = set(role_mapping.get("cash", set()))
    custom_mapping = _execute(conn, 
        "SELECT meta_value FROM management_dashboard_meta WHERE meta_key='roles_customized'"
    ).fetchone()
    if not cash_ids and not (custom_mapping and custom_mapping["meta_value"] == "1"):
        account_columns = table_columns(conn, "accounts")
        if "cash_flow_category" in account_columns:
            cash_ids = {r["id"] for r in _execute(conn, 
                """SELECT id FROM accounts WHERE is_group=0 AND
                   (cash_flow_category='cash_equivalents' OR code IN ('1110','1120')
                    OR name_ar LIKE '%صندوق%' OR name_ar LIKE '%بنك%')"""
            )}
        else:
            cash_ids = {r["id"] for r in _execute(conn, 
                "SELECT id FROM accounts WHERE is_group=0 AND (code IN ('1110','1120') OR name_ar LIKE '%صندوق%' OR name_ar LIKE '%بنك%')"
            )}
    mapping_exists = table_exists(conn, "cash_flow_account_mappings")
    cash_flow_mappings = {
        row["account_id"]: row["category"]
        for row in _execute(conn, "SELECT account_id,category FROM cash_flow_account_mappings")
    } if mapping_exists else {}

    # Current assets and current liabilities are operating working capital by
    # default under the indirect/direct cash-flow presentation. Infer this from
    # the account tree so new detail accounts do not remain "unmapped" or get
    # incorrectly classified as investing/financing solely by their code.
    inferred_operating_account_ids = set()
    account_columns = table_columns(conn, "accounts")
    if {"parent_id", "code", "name_ar", "is_group"}.issubset(account_columns):
        inferred_working_capital = _execute(conn, 
            """WITH RECURSIVE ancestry(account_id,parent_id,account_type,depth) AS (
                   SELECT id,parent_id,account_type,0 FROM accounts WHERE is_group=0
                   UNION ALL
                   SELECT ancestry.account_id,parent.parent_id,ancestry.account_type,depth+1
                   FROM ancestry JOIN accounts parent ON parent.id=ancestry.parent_id
                   WHERE parent.parent_id IS NOT NULL AND depth<8
               )
               SELECT DISTINCT ancestry.account_id,ancestry.account_type,grp.code,grp.name_ar
               FROM ancestry JOIN accounts grp ON grp.id=ancestry.parent_id
               WHERE grp.is_group=1"""
        ).fetchall()
        for row in inferred_working_capital:
            code = str(row["code"] or "")
            name = str(row["name_ar"] or "")
            is_current_asset = row["account_type"] == "asset" and (
                code == "1100" or "الأصول المتداولة" in name
            )
            is_current_liability = row["account_type"] == "liability" and (
                code == "2100" or "الالتزامات المتداولة" in name
            )
            if is_current_asset or is_current_liability:
                if row["account_id"] not in cash_flow_mappings:
                    inferred_operating_account_ids.add(row["account_id"])
                cash_flow_mappings.setdefault(row["account_id"], "operating")

    # Legacy/custom charts may not have parent_id relationships. In this chart,
    # 11xx detail assets and 21xx detail liabilities are current working capital.
    # Respect an explicit non-operating classification if the user configured one.
    for row in _execute(conn, 
        "SELECT id,code,account_type FROM accounts WHERE is_group=0"
    ).fetchall():
        code = str(row["code"] or "")
        is_current = (
            row["account_type"] == "asset" and code.startswith("11")
        ) or (
            row["account_type"] == "liability" and code.startswith("21")
        )
        if not is_current:
            continue
        explicit_category = cash_flow_mappings.get(row["id"])
        if explicit_category is None:
            inferred_operating_account_ids.add(row["id"])
            cash_flow_mappings[row["id"]] = "operating"

    unmapped_accounts = {}
    if not cash_ids:
        opening = closing = Decimal("0")
        sections = {k: {"in": Decimal("0"), "out": Decimal("0")} for k in ("operating", "investing", "financing")}
    else:
        marks = ",".join("?" for _ in cash_ids)
        ids = tuple(sorted(cash_ids))
        from management_dashboard import _copied_opening_entry_ids
        from datetime import date as date_type, timedelta
        opening_cutoff = (date_type.fromisoformat(start)-timedelta(days=1)).isoformat()
        copied_openings = sorted(_copied_opening_entry_ids(conn, opening_cutoff))
        opening_exclusion = (
            " AND je.id NOT IN (" + ",".join("?" for _ in copied_openings) + ")"
            if copied_openings else ""
        )
        opening_row = _execute(conn, f"""SELECT {decimal_net_expression(conn, "jl.debit", "jl.credit")} value
            FROM journal_lines jl JOIN journal_entries je ON je.id=jl.entry_id
            WHERE jl.account_id IN ({marks}) AND je.status='posted' AND je.entry_date<?
              {opening_exclusion}""",
            (*ids, start, *copied_openings)).fetchone()
        opening = Decimal(str(opening_row["value"] or "0"))
        sections = {k: {"in": Decimal("0"), "out": Decimal("0")} for k in ("operating", "investing", "financing")}
        journal_columns=table_columns(conn, "journal_entries")
        reference_expr="COALESCE(je.reference,'')" if "reference" in journal_columns else "''"
        copied_period_openings = sorted(_copied_opening_entry_ids(conn, end))
        period_opening_exclusion = (
            " AND je.id NOT IN (" + ",".join("?" for _ in copied_period_openings) + ")"
            if copied_period_openings else ""
        )
        entries = _execute(conn, f"""SELECT je.id,{reference_expr} reference,
            {decimal_net_expression(conn, f"CASE WHEN jl.account_id IN ({marks}) THEN jl.debit ELSE '0' END", f"CASE WHEN jl.account_id IN ({marks}) THEN jl.credit ELSE '0' END")} cash_net
            FROM journal_entries je JOIN journal_lines jl ON jl.entry_id=je.id
            WHERE je.status='posted' AND je.entry_date>=? AND je.entry_date<=?{period_opening_exclusion}
            GROUP BY je.id ORDER BY je.entry_date,je.id""",
            (*ids, *ids, start, end, *copied_period_openings)).fetchall()
        for entry in entries:
            net = Decimal(str(entry["cash_net"] or "0"))
            if not net:
                continue
            if str(entry["reference"] or "").startswith("ASSET-DISPOSAL-"):
                if net > 0:
                    sections["investing"]["in"] += net
                else:
                    sections["investing"]["out"] += -net
                continue
            # Supplier payments may settle several bills. Split the cash outflow
            # according to the actual bill allocations so asset acquisitions are
            # investing cash flows, not operating merely because AP was credited.
            payment_tables = {
                name for name in ("supplier_payments", "supplier_payment_allocations", "supplier_bills")
                if table_exists(conn, name)
            }
            if net < 0 and payment_tables == {
                "supplier_payments", "supplier_payment_allocations", "supplier_bills"
            }:
                payment = _execute(conn, 
                    "SELECT id FROM supplier_payments WHERE journal_entry_id=?", (entry["id"],)
                ).fetchone()
                if payment:
                    allocations = _execute(conn, f"""
                        SELECT {decimal_sum_expression(conn, "pa.amount")} total_allocated,
                               {decimal_sum_expression(conn, "CASE WHEN sb.bill_kind='asset' THEN pa.amount ELSE '0' END")} asset_allocated
                        FROM supplier_payment_allocations pa
                        JOIN supplier_bills sb ON sb.id=pa.bill_id
                        WHERE pa.payment_id=?
                    """, (payment["id"],)).fetchone()
                    total_allocated = Decimal(str(allocations["total_allocated"] or "0"))
                    asset_allocated = Decimal(str(allocations["asset_allocated"] or "0"))
                    if total_allocated > 0 and asset_allocated > 0:
                        investing_out = min(
                            abs(net),
                            (abs(net) * asset_allocated / total_allocated).quantize(
                                Decimal("0.01"), rounding=ROUND_HALF_UP
                            ),
                        )
                        sections["investing"]["out"] += investing_out
                        sections["operating"]["out"] += abs(net) - investing_out
                        continue
            account_columns = table_columns(conn, "accounts")
            section_expr = "a.statement_section" if "statement_section" in account_columns else "NULL"
            counterpart = _execute(conn, f"""SELECT jl.account_id,a.account_type,a.code,a.name_ar,
                {section_expr} statement_section,jl.debit,jl.credit
                FROM journal_lines jl JOIN accounts a ON a.id=jl.account_id
                WHERE jl.entry_id=? AND jl.account_id NOT IN ({marks})""",
                (entry["id"], *ids)).fetchall()
            weights = {"operating": Decimal("0"), "investing": Decimal("0"), "financing": Decimal("0")}
            for item in counterpart:
                code, name = item["code"], item["name_ar"]
                mapped_category = cash_flow_mappings.get(item["account_id"])
                if mapped_category:
                    category = mapped_category
                elif item["statement_section"] == "non_current_assets" or (
                    item["account_type"] == "asset" and not (
                        code.startswith(("11", "21")) or "عميل" in name or "مخزون" in name or "مقدم" in name
                    )
                ):
                    category = "investing"
                elif item["account_type"] == "equity" or item["statement_section"] == "non_current_liabilities" or (
                    item["account_type"] == "liability" and (code.startswith("22") or "قرض" in name)
                ):
                    category = "financing"
                else:
                    category = "operating"
                if not mapped_category:
                    unmapped_accounts[item["account_id"]] = {"code": code, "name": name, "suggested": category}
                weights[category] += abs(Decimal(str(item["debit"] or "0"))) + abs(Decimal(str(item["credit"] or "0")))
            total_weight = sum(weights.values(), Decimal("0"))
            if not total_weight:
                weights["operating"] = Decimal("1")
                total_weight = Decimal("1")
            remaining = abs(net)
            active = [k for k, value in weights.items() if value]
            for category in active[:-1]:
                part = (abs(net) * weights[category] / total_weight).quantize(CENT, rounding=ROUND_HALF_UP)
                remaining -= part
                if net > 0:
                    sections[category]["in"] += part
                else:
                    sections[category]["out"] += part
            category = active[-1]
            if net > 0:
                sections[category]["in"] += remaining
            else:
                sections[category]["out"] += remaining
        movement = sum((item["in"] - item["out"] for item in sections.values()), Decimal("0"))
        closing = opening + movement
    total_in = sum((item["in"] for item in sections.values()), Decimal("0"))
    total_out = sum((item["out"] for item in sections.values()), Decimal("0"))
    net_change = closing - opening
    reconciliation_difference = net_change - (total_in - total_out)
    return {
        "date_from": start, "date_to": end, "opening": opening, "closing": closing,
        "sections": sections, "total_in": total_in, "total_out": total_out,
        "net_change": net_change, "reconciliation_difference": reconciliation_difference,
        "cash_mapping_available": bool(cash_ids),
        "cash_reconciles": bool(cash_ids) and reconciliation_difference == 0,
        "unmapped_accounts": sorted(unmapped_accounts.values(), key=lambda item: item["code"]),
        "unmapped_account_count": len(unmapped_accounts),
        "inferred_operating_account_ids": sorted(inferred_operating_account_ids),
    }


def build_cash_flow_indirect_snapshot(conn, start, end):
    """Prepare the indirect IAS 7 operating section, retaining cash reconciliation."""
    direct = build_cash_flow_snapshot(conn, start, end)
    from management_dashboard import load_management_account_roles
    roles = load_management_account_roles(conn)
    cash_ids = roles.get("cash", set())
    if not cash_ids:
        return {
            **direct, "method": "indirect", "indirect_items": [],
            "indirect_operating_net": Decimal("0"),
            "indirect_reconciliation": direct["sections"]["operating"]["in"] - direct["sections"]["operating"]["out"],
            "indirect_mapping_warning": True,
        }

    def balances_as_of(as_of):
        # Keep working-capital movements consistent with the formal balance
        # sheet: later-year opening copies must not duplicate prior ledger history.
        from management_dashboard import _copied_opening_entry_ids
        excluded = sorted(_copied_opening_entry_ids(conn, as_of))
        exclusion_sql = (
            " AND je.id NOT IN (" + ",".join("?" for _ in excluded) + ")"
            if excluded else ""
        )
        rows = _execute(conn, f"""
            SELECT a.id,a.account_type,
                   {decimal_sum_expression(conn, "CASE WHEN je.id IS NOT NULL THEN jl.debit ELSE '0' END")} debit,
                   {decimal_sum_expression(conn, "CASE WHEN je.id IS NOT NULL THEN jl.credit ELSE '0' END")} credit
            FROM accounts a
            LEFT JOIN journal_lines jl ON jl.account_id=a.id
            LEFT JOIN journal_entries je ON je.id=jl.entry_id
                 AND je.status='posted' AND je.entry_date<=?{exclusion_sql}
            WHERE a.is_group=0
            GROUP BY a.id,a.account_type
        """, (as_of, *excluded)).fetchall()
        result = {}
        for row in rows:
            debit, credit = Decimal(str(row["debit"])), Decimal(str(row["credit"]))
            result[row["id"]] = debit-credit if row["account_type"] == "asset" else credit-debit
        return result

    from datetime import date as date_type, timedelta
    opening_balances = balances_as_of((date_type.fromisoformat(start)-timedelta(days=1)).isoformat())
    closing_balances = balances_as_of(end)
    def role_balance(account_ids, balances):
        return sum((balances.get(account_id, Decimal("0")) for account_id in account_ids), Decimal("0"))

    entry_columns = table_columns(conn, "journal_entries")
    closing_filter = "AND COALESCE(je.entry_type,'general') NOT IN ('year_end_closing','year_end_closing_reversal')" if "entry_type" in entry_columns else ""
    period_rows = _execute(conn, f"""
        SELECT a.id account_id,a.account_type,a.code,a.name_ar,
               {decimal_sum_expression(conn, "jl.debit")} debit,
               {decimal_sum_expression(conn, "jl.credit")} credit
        FROM journal_lines jl
        JOIN journal_entries je ON je.id=jl.entry_id
        JOIN accounts a ON a.id=jl.account_id
        WHERE je.status='posted' AND je.entry_date>=? AND je.entry_date<=?
          AND a.is_group=0 {closing_filter}
        GROUP BY a.id,a.account_type,a.code,a.name_ar
    """, (start,end)).fetchall()
    revenue = sum((Decimal(str(r["credit"]))-Decimal(str(r["debit"]))
                   for r in period_rows if r["account_type"]=="revenue"), Decimal("0"))
    expenses = sum((Decimal(str(r["debit"]))-Decimal(str(r["credit"]))
                    for r in period_rows if r["account_type"]=="expense"), Decimal("0"))
    net_profit = revenue-expenses
    # Do not infer depreciation from fixed_assets.expense_account_id: older and
    # user-configured asset records may point at a general/admin expense account.
    # Only posted asset_depreciations rows and clearly identified depreciation
    # accounts are eligible for the non-cash add-back below.
    depreciation_account_ids=set()
    asset_depreciation_total=Decimal("0")
    depreciation_entry_ids=set()
    asset_depreciation_exists=table_exists(conn, "asset_depreciations")
    if asset_depreciation_exists:
        asset_depreciation_columns=table_columns(conn, "asset_depreciations")
        if {"amount","journal_entry_id"}.issubset(asset_depreciation_columns):
            posted_asset_depreciation=_execute(conn, """
                SELECT d.amount,d.journal_entry_id FROM asset_depreciations d
                JOIN journal_entries je ON je.id=d.journal_entry_id
                WHERE je.status='posted' AND je.entry_date>=? AND je.entry_date<=?
            """,(start,end)).fetchall()
            asset_depreciation_total=sum(
                (Decimal(str(row["amount"] or "0")) for row in posted_asset_depreciation),
                Decimal("0"),
            )
            depreciation_entry_ids={
                int(row["journal_entry_id"]) for row in posted_asset_depreciation
                if row["journal_entry_id"] is not None
            }
    # Add back all depreciation-account postings, but exclude journal entries
    # already represented by asset_depreciations.amount. Excluding an entire
    # account would hide separate/manual depreciation postings to that account.
    depreciation_candidate_ids=set(depreciation_account_ids)
    depreciation_candidate_ids.update(
        row["account_id"] for row in period_rows
        if row["account_type"]=="expense"
        and (str(row["code"]).startswith(("64","65"))
             or "إهلاك" in str(row["name_ar"])
             or "depreciation" in str(row["name_ar"]).lower())
    )
    other_depreciation=Decimal("0")
    if depreciation_candidate_ids:
        account_marks=",".join("?" for _ in depreciation_candidate_ids)
        entry_exclusion_sql=(
            " AND je.id NOT IN ("+",".join("?" for _ in depreciation_entry_ids)+")"
            if depreciation_entry_ids else ""
        )
        depreciation_row=_execute(conn, f"""
            SELECT {decimal_sum_expression(conn, "jl.debit")} debit,
                   {decimal_sum_expression(conn, "jl.credit")} credit
            FROM journal_lines jl
            JOIN journal_entries je ON je.id=jl.entry_id
            JOIN accounts a ON a.id=jl.account_id
            WHERE je.status='posted' AND je.entry_date>=? AND je.entry_date<=?
              AND a.is_group=0 AND a.account_type='expense'
              AND a.id IN ({account_marks}) {closing_filter}{entry_exclusion_sql}
        """,(start,end,*sorted(depreciation_candidate_ids),*sorted(depreciation_entry_ids))).fetchone()
        other_depreciation=(
            Decimal(str(depreciation_row["debit"] or "0"))
            -Decimal(str(depreciation_row["credit"] or "0"))
        )
    depreciation=asset_depreciation_total+other_depreciation
    disposal_gain_loss=Decimal("0")
    disposals_exists=table_exists(conn, "asset_disposals")
    if disposals_exists:
        disposal_columns=table_columns(conn, "asset_disposals")
        if {"gain_loss_amount","disposal_date"}.issubset(disposal_columns):
            disposal_row=_execute(conn, 
                f"SELECT {decimal_sum_expression(conn, 'gain_loss_amount')} amount FROM asset_disposals WHERE disposal_date>=? AND disposal_date<=?",
                (start,end),
            ).fetchone()
            disposal_gain_loss=Decimal(str(disposal_row["amount"] or "0"))
    disposal_adjustment=-disposal_gain_loss

    receivables_ids = set(roles.get("receivables", set()))
    inventory_ids = set(roles.get("inventory", set()))
    payables_ids = set(roles.get("payables", set()))
    current_asset_ids = set(roles.get("current_assets", set()))
    current_liability_ids = set(roles.get("current_liabilities", set()))

    # The cash-flow mapping screen classifies non-cash accounts as operating,
    # investing or financing. For the indirect method, explicitly mapped
    # operating assets/liabilities are also working-capital accounts; include
    # them in balance-change adjustments instead of silently omitting them.
    # First infer current working-capital accounts from the chart hierarchy.
    # This covers detailed accounts below the current-asset/current-liability
    # parent groups even when an explicit cash-flow role has not been assigned.
    account_columns = table_columns(conn, "accounts")
    if {"parent_id", "code", "name_ar", "is_group"}.issubset(account_columns):
        current_group_rows = _execute(conn, 
            """WITH RECURSIVE ancestry(account_id,parent_id,account_type,depth) AS (
                   SELECT id,parent_id,account_type,0 FROM accounts WHERE is_group=0
                   UNION ALL
                   SELECT ancestry.account_id,parent.parent_id,ancestry.account_type,depth+1
                   FROM ancestry JOIN accounts parent ON parent.id=ancestry.parent_id
                   WHERE parent.parent_id IS NOT NULL AND depth<8
               )
               SELECT DISTINCT ancestry.account_id,ancestry.account_type,grp.code,grp.name_ar
               FROM ancestry JOIN accounts grp ON grp.id=ancestry.parent_id
               WHERE grp.is_group=1"""
        ).fetchall()
        for ancestor in current_group_rows:
            code = str(ancestor["code"] or "")
            name = str(ancestor["name_ar"] or "")
            if ancestor["account_type"] == "asset" and (
                code == "1100" or "الأصول المتداولة" in name
            ):
                current_asset_ids.add(ancestor["account_id"])
            elif ancestor["account_type"] == "liability" and (
                code == "2100" or "الالتزامات المتداولة" in name
            ):
                current_liability_ids.add(ancestor["account_id"])

    # Explicit operating mappings are an additional source for custom charts
    # whose parent groups do not use standard current-asset/current-liability
    # codes or names.
    mapping_table = table_exists(conn, "cash_flow_account_mappings")
    if mapping_table:
        mapped_operating = _execute(conn, 
            """SELECT m.account_id,a.account_type FROM cash_flow_account_mappings m
               JOIN accounts a ON a.id=m.account_id
               WHERE m.category='operating' AND a.is_group=0"""
        ).fetchall()
        for mapped in mapped_operating:
            if mapped["account_type"] == "asset":
                current_asset_ids.add(mapped["account_id"])
            elif mapped["account_type"] == "liability":
                current_liability_ids.add(mapped["account_id"])

    # Fallback for charts where detailed current liabilities use the standard
    # 21xx range but are not linked to a 2100 parent (e.g. payroll payable/GRNI).
    mapping_table = table_exists(conn, "cash_flow_account_mappings")
    configured_categories = {
        row["account_id"]: row["category"]
        for row in _execute(conn, "SELECT account_id,category FROM cash_flow_account_mappings")
    } if mapping_table else {}
    for row in _execute(conn, 
        "SELECT id,code,name_ar,account_type FROM accounts WHERE is_group=0 AND account_type='liability'"
    ).fetchall():
        code = str(row["code"] or "")
        name = str(row["name_ar"] or "")
        is_current_liability = code.startswith("21") or "متداولة" in name
        if is_current_liability and configured_categories.get(row["id"], "operating") == "operating":
            current_liability_ids.add(row["id"])

    other_current_asset_ids = current_asset_ids-cash_ids-receivables_ids-inventory_ids
    other_current_liability_ids = current_liability_ids-payables_ids

    changes = {
        "receivables": role_balance(receivables_ids, closing_balances)-role_balance(receivables_ids, opening_balances),
        "inventory": role_balance(inventory_ids, closing_balances)-role_balance(inventory_ids, opening_balances),
        "other_current_assets": role_balance(other_current_asset_ids, closing_balances)-role_balance(other_current_asset_ids, opening_balances),
        "payables": role_balance(payables_ids, closing_balances)-role_balance(payables_ids, opening_balances),
        "other_current_liabilities": role_balance(other_current_liability_ids, closing_balances)-role_balance(other_current_liability_ids, opening_balances),
    }
    calculated_operating = (
        net_profit + depreciation + disposal_adjustment
        - changes["receivables"] - changes["inventory"]
        - changes["other_current_assets"] + changes["payables"]
        + changes["other_current_liabilities"]
    )
    investing_net = direct["sections"]["investing"]["in"]-direct["sections"]["investing"]["out"]
    financing_net = direct["sections"]["financing"]["in"]-direct["sections"]["financing"]["out"]
    actual_operating = direct["net_change"]-investing_net-financing_net
    # Never force the indirect method to balance through an unexplained plug.
    # Keep the calculated operating cash flow and disclose the classification
    # difference so missing adjustments/mappings remain visible to the reviewer.
    unexplained_difference = actual_operating-calculated_operating
    operating_net = calculated_operating

    def signed_section(value):
        return {"in": max(value, Decimal("0")), "out": max(-value, Decimal("0"))}
    sections = {
        "operating": signed_section(operating_net),
        "investing": direct["sections"]["investing"],
        "financing": direct["sections"]["financing"],
    }
    indirect_items = [
        {"label":"صافي الربح قبل التدفقات النقدية", "amount":net_profit, "kind":"profit"},
        {"label":"إضافة الإهلاك والمصروفات غير النقدية المحددة", "amount":depreciation, "kind":"adjustment"},
        {"label":"استبعاد أرباح / خسائر بيع الأصول من الربح", "amount":disposal_adjustment, "kind":"adjustment"},
        {"label":"التغير في العملاء والمدينين", "amount":-changes["receivables"], "kind":"working_capital"},
        {"label":"التغير في المخزون", "amount":-changes["inventory"], "kind":"working_capital"},
        {"label":"التغير في الأصول المتداولة الأخرى", "amount":-changes["other_current_assets"], "kind":"working_capital"},
        {"label":"التغير في الموردين والدائنين", "amount":changes["payables"], "kind":"working_capital"},
        {"label":"التغير في الالتزامات المتداولة الأخرى", "amount":changes["other_current_liabilities"], "kind":"working_capital"},
    ]
    total_in = sum((item["in"] for item in sections.values()), Decimal("0"))
    total_out = sum((item["out"] for item in sections.values()), Decimal("0"))
    section_net = total_in-total_out
    reconciliation_difference = direct["net_change"]-section_net
    return {
        **direct, "method":"indirect", "net_profit":net_profit, "sections":sections,
        "total_in":total_in, "total_out":total_out,
        "reconciliation_difference":reconciliation_difference,
        "cash_reconciles":bool(direct["cash_mapping_available"]) and reconciliation_difference == 0,
        "indirect_items":indirect_items,
        "indirect_operating_net":operating_net,
        "indirect_reconciliation":unexplained_difference,
        "indirect_mapping_warning":not bool(current_asset_ids and current_liability_ids),
    }


@planning_bp.route("/settings/cash-flow-mapping", methods=["GET", "POST"])
@_require("users.manage")
def cash_flow_mapping():
    conn = _connect()
    try:
        initialize_planning_schema(conn)
        if request.method == "POST":
            rows = _execute(conn, 
                "SELECT id FROM accounts WHERE is_group=0 AND active=1"
            ).fetchall()
            allowed_ids = {str(row["id"]) for row in rows}
            categories = {"operating", "investing", "financing"}
            begin_write_transaction(conn)
            _execute(conn, "DELETE FROM cash_flow_account_mappings")
            for account_id in sorted(allowed_ids, key=int):
                category = request.form.get(f"category_{account_id}", "").strip()
                if category in categories:
                    _execute(conn, 
                        """INSERT INTO cash_flow_account_mappings(account_id,category)
                           VALUES(?,?)""", (int(account_id), category)
                    )
            conn.commit()
            flash("تم حفظ تصنيف حسابات التدفقات النقدية. الحسابات غير المحددة ستظهر في تقرير المراجعة.", "success")
            return redirect(url_for("planning.cash_flow_mapping"))
        accounts = _execute(conn, """
            SELECT a.id,a.code,a.name_ar,a.account_type,m.category
            FROM accounts a
            LEFT JOIN cash_flow_account_mappings m ON m.account_id=a.id
            WHERE a.is_group=0 AND a.active=1
            ORDER BY a.code
        """).fetchall()
        cash_roles = set()
        try:
            from management_dashboard import load_management_account_roles
            cash_roles = set(load_management_account_roles(conn).get("cash", set()))
        except (*DB_ERROR, KeyError, ValueError):
            pass
        return render_template(
            "cash_flow_mapping.html", accounts=accounts,
            cash_account_ids=cash_roles,
            categories={
                "operating":"تشغيلي", "investing":"استثماري", "financing":"تمويلي"
            }
        )
    finally:
        conn.close()


@planning_bp.route("/reports/cash-flow")
@_require("cashflow.view")
def cash_flow():
    from datetime import date
    raw_from = request.args.get("date_from", "").strip()
    raw_to = request.args.get("date_to", "").strip()
    today = date.today()
    try:
        start = date.fromisoformat(raw_from) if raw_from else today.replace(day=1)
        end = date.fromisoformat(raw_to) if raw_to else today
        if end < start:
            raise ValueError()
    except ValueError:
        flash("فترة التقرير غير صحيحة.", "error")
        start, end = today.replace(day=1), today
    method = request.args.get("method", "indirect").strip().lower()
    if method not in {"direct", "indirect"}:
        method = "indirect"
    conn = _connect()
    try:
        builder = build_cash_flow_indirect_snapshot if method == "indirect" else build_cash_flow_snapshot
        snapshot = builder(conn, start.isoformat(), end.isoformat())
        snapshot["method"] = method
    finally:
        conn.close()
    if not snapshot["cash_mapping_available"]:
        flash("حدد حسابات الصندوق والبنوك من شاشة ربط حسابات لوحة الإدارة أولًا.", "warning")
    return render_template("cash_flow.html", **snapshot)


__all__=["planning_bp","initialize_planning_schema","create_financial_year","list_financial_years","create_opening_entry","close_financial_year","reverse_financial_year_entry","build_balance_sheet_snapshot","build_cash_flow_snapshot","build_cash_flow_indirect_snapshot","cash_flow_mapping"]
