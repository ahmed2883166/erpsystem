"""Budget planning and cash-flow reporting for NEXUS ERP."""
from app_paths import DB_PATH
from database import connect_database
import sqlite3
from accounting_core import post_journal, register_decimal_aggregates, reconciliation_snapshot
from chart_templates import ACTIVITY_ACCOUNT_ROLES
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import wraps
from flask import Blueprint, abort, flash, redirect, render_template, request, session, url_for

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


def initialize_planning_schema(conn):
    conn.executescript("""
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
    journal_table_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='journal_entries'"
    ).fetchone()
    if journal_table_exists:
        if "entry_type" not in {row["name"] for row in conn.execute("PRAGMA table_info(journal_entries)")}:
            conn.execute("ALTER TABLE journal_entries ADD COLUMN entry_type TEXT NOT NULL DEFAULT 'general'")
        # Migrate historical closing journals using the dedicated history table.
        conn.execute("""UPDATE journal_entries SET entry_type='year_end_closing'
            WHERE id IN (SELECT entry_id FROM financial_year_journal_history
                         WHERE entry_kind='closing' AND action='created')""")
        conn.execute("""UPDATE journal_entries SET entry_type='year_end_closing_reversal'
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
    conn.execute("BEGIN IMMEDIATE")
    try:
        overlap = conn.execute(
            """SELECT year_name FROM financial_years
               WHERE start_date <= ? AND end_date >= ? LIMIT 1""",
            (end, start),
        ).fetchone()
        if overlap:
            raise ValueError(f"الفترة تتداخل مع السنة المالية: {overlap[0]}.")
        cur = conn.execute(
            """INSERT INTO financial_years(year_name,start_date,end_date,created_by)
               VALUES(?,?,?,?)""",
            (name, start, end, created_by),
        )
        conn.commit()
        return cur.lastrowid
    except Exception:
        conn.rollback()
        raise


def list_financial_years(conn):
    """Return financial years in descending chronological order."""
    return conn.execute(
        "SELECT * FROM financial_years ORDER BY start_date DESC,id DESC"
    ).fetchall()

def _start_financial_year_transaction(conn):
    if conn.in_transaction:
        raise ValueError("هناك عملية قاعدة بيانات مفتوحة؛ احفظها أو أغلقها قبل تنفيذ الإجراء.")
    conn.execute("BEGIN IMMEDIATE")


def _financial_year_or_error(conn, year_id):
    year = conn.execute("SELECT * FROM financial_years WHERE id=?", (year_id,)).fetchone()
    if not year:
        raise ValueError("السنة المالية غير موجودة.")
    return year


def _history_sequence(conn, year_id, entry_kind, action):
    row = conn.execute("""SELECT COUNT(*) FROM financial_year_journal_history
        WHERE financial_year_id=? AND entry_kind=? AND action=?""",
        (year_id, entry_kind, action)).fetchone()
    return int(row[0]) + 1


def _previous_closed_financial_year(conn, start_date):
    return conn.execute("""SELECT * FROM financial_years
        WHERE end_date < ? ORDER BY end_date DESC,id DESC LIMIT 1""",
        (start_date,)).fetchone()


def _opening_balance_mismatches(conn, cutoff_date, normalized):
    """Compare opening balances with the ledger at the day before the new year starts.

    Any unclosed prior-period profit/loss is included in the retained-earnings
    account, so an unclosed previous financial year cannot bypass validation.
    """
    account_rows = conn.execute("""SELECT id,code,name_ar,account_type FROM accounts
        WHERE account_type IN ('asset','liability','equity') AND is_group=0""").fetchall()
    expected_balances = {row["id"]: Decimal("0") for row in account_rows}
    metadata = {row["id"]: row for row in account_rows}
    rows = conn.execute("""SELECT jl.account_id,a.account_type,jl.debit,jl.credit
        FROM journal_lines jl
        JOIN journal_entries je ON je.id=jl.entry_id
        JOIN accounts a ON a.id=jl.account_id
        WHERE je.status='posted' AND je.entry_date<=?
          AND a.account_type IN ('asset','liability','equity') AND a.is_group=0
        ORDER BY jl.account_id,je.id,jl.id""", (cutoff_date,)).fetchall()
    for row in rows:
        account_id = row["account_id"]
        debit = Decimal(str(row["debit"] or "0"))
        credit = Decimal(str(row["credit"] or "0"))
        expected_balances[account_id] += (
            debit - credit if row["account_type"] == "asset" else credit - debit
        )

    # If prior P&L has not yet been closed, carry its net result into retained earnings
    # for comparison with the opening balance entered for the new financial year.
    pnl_rows = conn.execute("""SELECT a.account_type,jl.debit,jl.credit
        FROM journal_lines jl
        JOIN journal_entries je ON je.id=jl.entry_id
        JOIN accounts a ON a.id=jl.account_id
        WHERE je.status='posted' AND je.entry_date<=?
          AND a.account_type IN ('revenue','expense') AND a.is_group=0""",
        (cutoff_date,)).fetchall()
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
            account = conn.execute("SELECT id,code,name_ar,account_type FROM accounts WHERE id=?",
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
        row = conn.execute("""SELECT setting_value FROM company_settings
            WHERE setting_key='activity' LIMIT 1""").fetchone()
        if row and row["setting_value"] in ACTIVITY_ACCOUNT_ROLES:
            activity = row["setting_value"]
    role_codes = ACTIVITY_ACCOUNT_ROLES.get(activity, {}).get("retained_earnings", ())
    if isinstance(role_codes, str):
        role_codes = (role_codes,)
    for code in role_codes:
        account = conn.execute("""SELECT id FROM accounts
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
        posted_before_opening = conn.execute("""SELECT COUNT(*) FROM journal_entries
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
        accounts = conn.execute(
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
        prior_posted = conn.execute("""SELECT COUNT(*) FROM journal_entries
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
        conn.execute("UPDATE financial_years SET opening_entry_id=? WHERE id=?", (entry_id, year_id))
        conn.execute("""INSERT INTO financial_year_journal_history
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

    drafts = conn.execute("""SELECT COUNT(*) FROM journal_entries
        WHERE status='draft' AND entry_date>=? AND entry_date<=?""", (start, end)).fetchone()[0]
    add("draft_entries", "قيود مسودة داخل السنة", "danger" if drafts else "success",
        drafts, "يجب معالجة القيود المسودة قبل الإقفال.", drafts > 0)

    register_decimal_aggregates(conn)
    grouped_totals = conn.execute("""SELECT je.id,
            COALESCE(DECIMAL_SUM(jl.debit),'0') debit,
            COALESCE(DECIMAL_SUM(jl.credit),'0') credit
        FROM journal_entries je LEFT JOIN journal_lines jl ON jl.entry_id=je.id
        WHERE je.status='posted' AND je.entry_date>=? AND je.entry_date<=?
        GROUP BY je.id""", (start, end)).fetchall()
    unbalanced = sum(
        Decimal(str(row["debit"] or "0")) != Decimal(str(row["credit"] or "0"))
        for row in grouped_totals
    )
    add("unbalanced_entries", "قيود مرحلة غير متوازنة", "danger" if unbalanced else "success",
        unbalanced, "راجع القيود وصححها بقيود تسوية معتمدة.", unbalanced > 0)

    empty = conn.execute("""SELECT COUNT(*) FROM journal_entries je
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
        opening = conn.execute("""SELECT je.entry_date,je.status,
                COUNT(jl.id) line_count,
                COALESCE(DECIMAL_SUM(jl.debit),'0') debit,
                COALESCE(DECIMAL_SUM(jl.credit),'0') credit,
                COALESCE(SUM(CASE WHEN a.account_type NOT IN ('asset','liability','equity')
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

    invalid_account_lines = conn.execute("""SELECT COUNT(*) FROM journal_lines jl
        JOIN journal_entries je ON je.id=jl.entry_id
        JOIN accounts a ON a.id=jl.account_id
        WHERE je.status='posted' AND je.entry_date>=? AND je.entry_date<=?
          AND (a.is_group=1 OR a.active=0)""", (start, end)).fetchone()[0]
    add("invalid_account_lines", "حركات على حسابات تجميعية أو غير نشطة",
        "danger" if invalid_account_lines else "success", invalid_account_lines,
        "يجب تصحيح الحركات المرتبطة بحساب تجميعي أو غير نشط قبل الإقفال.",
        invalid_account_lines > 0)

    if _table_exists_financial(conn, "accounting_periods"):
        locked_period = conn.execute("""SELECT period_name FROM accounting_periods
            WHERE locked=1 AND start_date<=? AND end_date>=? LIMIT 1""", (end, end)).fetchone()
        add("year_end_period", "الفترة المحاسبية في تاريخ نهاية السنة",
            "danger" if locked_period else "success", 1 if locked_period else 0,
            f"الفترة {locked_period['period_name']} مغلقة بالفعل في تاريخ نهاية السنة." if locked_period else
            "لا توجد فترة مقفولة تمنع إجراء الإقفال.", bool(locked_period))

    if _table_exists_financial(conn, "bank_transactions"):
        bank_columns = {row["name"] for row in conn.execute("PRAGMA table_info(bank_transactions)")}
        if {"transaction_date", "reconciled"}.issubset(bank_columns):
            unreconciled_bank = conn.execute("""SELECT COUNT(*) FROM bank_transactions
                WHERE transaction_date>=? AND transaction_date<=? AND reconciled=0""",
                (start, end)).fetchone()[0]
            add("bank_reconciliation", "حركات بنكية غير مسوّاة", "warning" if unreconciled_bank else "success",
                unreconciled_bank, "راجع المطابقة البنكية للحركات داخل السنة قبل اعتماد الإقفال.", False)

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
    except (sqlite3.Error, KeyError, TypeError, ValueError):
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
        drafts = conn.execute("""SELECT COUNT(*) FROM journal_entries
            WHERE status='draft' AND entry_date>=? AND entry_date<=?""",
            (year["start_date"], year["end_date"])).fetchone()[0]
        if drafts:
            raise ValueError(f"لا يمكن الإقفال؛ توجد {drafts} قيود مسودة داخل السنة المالية.")
        if _table_exists_financial(conn, "accounting_periods"):
            locked = conn.execute("""SELECT period_name FROM accounting_periods
                WHERE locked=1 AND start_date<=? AND end_date>=? LIMIT 1""",
                (year["end_date"], year["end_date"])).fetchone()
            if locked:
                raise ValueError(f"لا يمكن الإقفال؛ الفترة المحاسبية {locked['period_name']} مغلقة بالفعل.")
        rows = conn.execute("""SELECT a.id,a.code,a.name_ar,a.account_type,a.is_group,a.active,
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
        conn.execute("UPDATE journal_entries SET entry_type='year_end_closing' WHERE id=?", (entry_id,))
        conn.execute("UPDATE financial_years SET closing_entry_id=?,status='closed' WHERE id=?",
                     (entry_id, year_id))
        conn.execute("""INSERT INTO financial_year_journal_history
            (financial_year_id,entry_kind,action,entry_id,created_by)
            VALUES(?,'closing','created',?,?)""", (year_id, entry_id, created_by))
        if preclose["warnings"] and allow_warnings and _table_exists_financial(conn, "audit_log"):
            conn.execute("""INSERT INTO audit_log(user_id,action,details,created_at)
                VALUES(?,?,?,CURRENT_TIMESTAMP)""",
                (created_by, "financial_year_warnings_approved",
                 f"اعتماد تحذيرات الإقفال للسنة {year_id} — قيد الإقفال {entry_id}"))
        conn.commit()
        return entry_id
    except Exception:
        conn.rollback()
        raise


def _table_exists_financial(conn, table_name):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (table_name,)).fetchone() is not None


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
            drafts = conn.execute("""SELECT COUNT(*) FROM journal_entries
                WHERE status='draft' AND entry_date>=? AND entry_date<=?""",
                (year["start_date"], year["end_date"])).fetchone()[0]
            if drafts:
                raise ValueError("لا يمكن عكس الإقفال قبل معالجة القيود المسودة داخل السنة.")
            later_year = conn.execute("""SELECT fy.year_name FROM financial_years fy
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
            later_entries = conn.execute("""SELECT id,entry_date FROM journal_entries
                WHERE entry_date>? AND status IN ('draft','posted')
                ORDER BY entry_date,id LIMIT 1""", (year["end_date"],)).fetchone()
            if later_entries:
                raise ValueError(
                    f"لا يمكن عكس إقفال السنة؛ يوجد قيد رقم {later_entries['id']} "
                    f"بتاريخ {later_entries['entry_date']} بعد نهاية السنة. "
                    "يجب معالجة القيود اللاحقة أولًا."
                )
            conn.execute("UPDATE financial_years SET status='open' WHERE id=?", (year_id,))
        original = conn.execute("SELECT id,description,status FROM journal_entries WHERE id=?",
                                 (original_id,)).fetchone()
        if not original or original["status"] != "posted":
            raise ValueError("القيد الأصلي غير موجود أو غير مرحّل.")
        original_lines = conn.execute("""SELECT account_id,debit,credit,memo
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
            conn.execute("UPDATE journal_entries SET entry_type='year_end_closing_reversal' WHERE id=?", (reversal_id,))
        else:
            conn.execute("UPDATE journal_entries SET entry_type='opening_reversal' WHERE id=?", (reversal_id,))
        if entry_kind == "closing":
            conn.execute("UPDATE financial_years SET closing_entry_id=NULL,status='open' WHERE id=?", (year_id,))
        else:
            conn.execute("UPDATE financial_years SET opening_entry_id=NULL WHERE id=?", (year_id,))
        conn.execute("""INSERT INTO financial_year_journal_history
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
        except (ValueError, sqlite3.IntegrityError) as exc:
            flash(str(exc) or "تعذر إنشاء السنة المالية؛ تحقق من الاسم والتواريخ.", "error")
        finally:
            conn.close()
        return redirect(url_for("planning.financial_years"))
    years = list_financial_years(conn)
    history = conn.execute("""SELECT h.id,h.entry_kind,h.action,h.entry_id,
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
        accounts = conn.execute("""SELECT id,code,name_ar,account_type FROM accounts
            WHERE active=1 AND is_group=0 AND account_type IN ('asset','liability','equity')
            ORDER BY CASE account_type WHEN 'asset' THEN 1 WHEN 'liability' THEN 2 ELSE 3 END,code""").fetchall()
        if request.method == "POST":
            balances = {str(account["id"]): request.form.get("balance_" + str(account["id"]), "0")
                        for account in accounts}
            entry_id = create_opening_entry(conn, year_id, balances, session.get("user_id"))
            flash(f"تم ترحيل قيد الافتتاح رقم {entry_id} بتاريخ {year['start_date']}.", "success")
            return redirect(url_for("planning.financial_years"))
        return render_template("financial_year_opening.html", year=year, accounts=accounts)
    except (ValueError, sqlite3.IntegrityError) as exc:
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
    except (ValueError, sqlite3.IntegrityError) as exc:
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
    except (ValueError, sqlite3.IntegrityError) as exc:
        flash(str(exc) or "تعذر عكس القيد.", "error")
    finally:
        conn.close()
    return redirect(url_for("planning.financial_years"))


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
            ids = request.form.getlist("account_id")
            if not ids:
                raise ValueError("أضف حسابًا واحدًا على الأقل وحدد موازنته.")
            lines = []
            for account_id in ids:
                raw_amount = request.form.get("amount_" + str(account_id), "0")
                account = conn.execute("SELECT id,account_type,is_group,active FROM accounts WHERE id=?", (int(account_id),)).fetchone()
                if not account or account["is_group"] or not account["active"] or account["account_type"] not in {"revenue","expense"}:
                    raise ValueError("الموازنة تقبل حسابات الإيرادات والمصروفات التفصيلية النشطة فقط.")
                lines.append((int(account_id), str(_money(raw_amount))))
            if len({x[0] for x in lines}) != len(lines):
                raise ValueError("لا تكرر الحساب داخل الموازنة نفسها.")
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute("INSERT INTO budget_plans(name,date_from,date_to,created_by) VALUES(?,?,?,?)",
                               (name,start,end,session["user_id"]))
            conn.executemany("INSERT INTO budget_lines(budget_id,account_id,amount) VALUES(?,?,?)",
                             [(cur.lastrowid, aid, amount) for aid,amount in lines])
            conn.commit()
            flash("تم حفظ الموازنة بنجاح.", "success")
            return redirect(url_for("planning.budget_detail", budget_id=cur.lastrowid))
        except (ValueError, TypeError, InvalidOperation, sqlite3.IntegrityError) as exc:
            conn.rollback()
            flash(str(exc) or "تعذر حفظ الموازنة.", "error")
    plans = conn.execute("SELECT * FROM budget_plans ORDER BY date_from DESC,id DESC").fetchall()
    accounts = conn.execute("SELECT id,code,name_ar,account_type FROM accounts WHERE active=1 AND is_group=0 AND account_type IN ('revenue','expense') ORDER BY account_type,code").fetchall()
    conn.close()
    return render_template("budget.html", plans=plans, accounts=accounts)


@planning_bp.route("/budget/<int:budget_id>")
@_require("budget.view")
def budget_detail(budget_id):
    conn = _connect()
    plan = conn.execute("SELECT * FROM budget_plans WHERE id=?", (budget_id,)).fetchone()
    if not plan:
        conn.close(); abort(404)
    rows = conn.execute("""
      SELECT a.id,a.code,a.name_ar,a.account_type,b.amount,
        COALESCE((SELECT DECIMAL_NET(jl.credit,jl.debit) FROM journal_lines jl
          JOIN journal_entries je ON je.id=jl.entry_id
          WHERE jl.account_id=a.id AND je.status='posted'
            AND je.entry_date>=? AND je.entry_date<=?), '0') actual
      FROM budget_lines b JOIN accounts a ON a.id=b.account_id
      WHERE b.budget_id=? ORDER BY a.account_type,a.code
    """, (plan["date_from"],plan["date_to"],budget_id)).fetchall()
    results=[]
    for row in rows:
        budget=_money(row["amount"])
        raw=Decimal(str(row["actual"] or "0"))
        actual=raw if row["account_type"]=="revenue" else -raw
        variance=actual-budget
        results.append({**dict(row),"budget":budget,"actual":actual,"variance":variance,
                        "variance_pct":(variance*100/budget) if budget else None})
    conn.close()
    return render_template("budget_detail.html",plan=plan,rows=results,
                           revenue_budget=sum((x["budget"] for x in results if x["account_type"]=="revenue"),Decimal("0")),
                           revenue_actual=sum((x["actual"] for x in results if x["account_type"]=="revenue"),Decimal("0")),
                           expense_budget=sum((x["budget"] for x in results if x["account_type"]=="expense"),Decimal("0")),
                           expense_actual=sum((x["actual"] for x in results if x["account_type"]=="expense"),Decimal("0")))



def build_balance_sheet_snapshot(conn, as_of):
    rows = conn.execute("""SELECT a.id,a.code,a.name_ar,a.account_type,a.parent_id,a.is_group,
        COALESCE(DECIMAL_SUM(CASE WHEN je.id IS NOT NULL THEN jl.debit ELSE '0' END),0) debit,
        COALESCE(DECIMAL_SUM(CASE WHEN je.id IS NOT NULL THEN jl.credit ELSE '0' END),0) credit
      FROM accounts a
      LEFT JOIN journal_lines jl ON jl.account_id=a.id
      LEFT JOIN journal_entries je ON je.id=jl.entry_id AND je.status='posted' AND je.entry_date<=?
      GROUP BY a.id ORDER BY a.code""", (as_of,)).fetchall()
    accounts = {}
    for row in rows:
        debit=Decimal(str(row["debit"] or "0"))
        credit=Decimal(str(row["credit"] or "0"))
        kind=row["account_type"]
        balance=debit-credit if kind=="asset" or kind=="expense" else credit-debit
        accounts[row["id"]]={**dict(row),"balance":balance}
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
                flat.append({**item,"display_balance":value,"depth":depth})
                walk(item["id"],depth+1,seen|{item["id"]})
        walk(None)
        sections[kind]=flat
    assets=totals["asset"]
    liabilities=totals["liability"]
    equity=totals["equity"]
    total_equity=equity+net_profit
    return {"sections":sections,"total_assets":assets,"total_liabilities":liabilities,
            "equity_before_profit":equity,"net_profit":net_profit,"total_equity":total_equity,
            "balance_difference":assets-liabilities-total_equity,
            "balanced":assets==liabilities+total_equity,"as_of":as_of}


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
    finally:
        conn.close()
    return render_template("balance_sheet.html",s=snapshot)


def build_cash_flow_snapshot(conn, start, end):
    """Build an IAS 7-oriented cash-flow snapshot for an inclusive date range."""
    from management_dashboard import load_management_account_roles
    role_mapping = load_management_account_roles(conn)
    cash_ids = set(role_mapping.get("cash", set()))
    custom_mapping = conn.execute(
        "SELECT meta_value FROM management_dashboard_meta WHERE meta_key='roles_customized'"
    ).fetchone()
    if not cash_ids and not (custom_mapping and custom_mapping["meta_value"] == "1"):
        cash_ids = {r["id"] for r in conn.execute(
            "SELECT id FROM accounts WHERE is_group=0 AND (code IN ('1110','1120') OR name_ar LIKE '%صندوق%' OR name_ar LIKE '%بنك%')"
        )}
    if not cash_ids:
        opening = closing = Decimal("0")
        sections = {k: {"in": Decimal("0"), "out": Decimal("0")} for k in ("operating", "investing", "financing")}
    else:
        marks = ",".join("?" for _ in cash_ids)
        ids = tuple(sorted(cash_ids))
        opening_row = conn.execute(f"""SELECT COALESCE(DECIMAL_NET(jl.debit,jl.credit),0) value
            FROM journal_lines jl JOIN journal_entries je ON je.id=jl.entry_id
            WHERE jl.account_id IN ({marks}) AND je.status='posted' AND je.entry_date<?""",
            (*ids, start)).fetchone()
        opening = Decimal(str(opening_row["value"] or "0"))
        sections = {k: {"in": Decimal("0"), "out": Decimal("0")} for k in ("operating", "investing", "financing")}
        entries = conn.execute(f"""SELECT je.id,
            COALESCE(DECIMAL_NET(CASE WHEN jl.account_id IN ({marks}) THEN jl.debit ELSE '0' END,
                                 CASE WHEN jl.account_id IN ({marks}) THEN jl.credit ELSE '0' END),0) cash_net
            FROM journal_entries je JOIN journal_lines jl ON jl.entry_id=je.id
            WHERE je.status='posted' AND je.entry_date>=? AND je.entry_date<=?
            GROUP BY je.id ORDER BY je.entry_date,je.id""",
            (*ids, *ids, start, end)).fetchall()
        for entry in entries:
            net = Decimal(str(entry["cash_net"] or "0"))
            if not net:
                continue
            counterpart = conn.execute(f"""SELECT a.account_type,a.code,a.name_ar,jl.debit,jl.credit
                FROM journal_lines jl JOIN accounts a ON a.id=jl.account_id
                WHERE jl.entry_id=? AND jl.account_id NOT IN ({marks})""",
                (entry["id"], *ids)).fetchall()
            weights = {"operating": Decimal("0"), "investing": Decimal("0"), "financing": Decimal("0")}
            for item in counterpart:
                code, name = item["code"], item["name_ar"]
                if item["account_type"] == "asset" and not (
                    code.startswith(("11", "21")) or "عميل" in name or "مخزون" in name or "مقدم" in name
                ):
                    category = "investing"
                elif item["account_type"] == "equity" or (
                    item["account_type"] == "liability" and (code.startswith("22") or "قرض" in name)
                ):
                    category = "financing"
                else:
                    category = "operating"
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
        rows = conn.execute("""
            SELECT a.id,a.account_type,
                   COALESCE(DECIMAL_SUM(CASE WHEN je.id IS NOT NULL THEN jl.debit ELSE '0' END),'0') debit,
                   COALESCE(DECIMAL_SUM(CASE WHEN je.id IS NOT NULL THEN jl.credit ELSE '0' END),'0') credit
            FROM accounts a
            LEFT JOIN journal_lines jl ON jl.account_id=a.id
            LEFT JOIN journal_entries je ON je.id=jl.entry_id
                 AND je.status='posted' AND je.entry_date<=?
            WHERE a.is_group=0
            GROUP BY a.id,a.account_type
        """, (as_of,)).fetchall()
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

    entry_columns = {row["name"] for row in conn.execute("PRAGMA table_info(journal_entries)")}
    closing_filter = "AND COALESCE(je.entry_type,'general') NOT IN ('year_end_closing','year_end_closing_reversal')" if "entry_type" in entry_columns else ""
    period_rows = conn.execute(f"""
        SELECT a.account_type,a.code,a.name_ar,
               COALESCE(DECIMAL_SUM(jl.debit),'0') debit,
               COALESCE(DECIMAL_SUM(jl.credit),'0') credit
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
    depreciation = sum((Decimal(str(r["debit"]))-Decimal(str(r["credit"]))
                        for r in period_rows if r["account_type"]=="expense"
                        and (str(r["code"]).startswith(("64","65"))
                             or "إهلاك" in str(r["name_ar"])
                             or "depreciation" in str(r["name_ar"]).lower())), Decimal("0"))

    receivables_ids = roles.get("receivables", set())
    inventory_ids = roles.get("inventory", set())
    payables_ids = roles.get("payables", set())
    current_asset_ids = roles.get("current_assets", set())
    current_liability_ids = roles.get("current_liabilities", set())
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
        net_profit + depreciation - changes["receivables"] - changes["inventory"]
        - changes["other_current_assets"] + changes["payables"]
        + changes["other_current_liabilities"]
    )
    investing_net = direct["sections"]["investing"]["in"]-direct["sections"]["investing"]["out"]
    financing_net = direct["sections"]["financing"]["in"]-direct["sections"]["financing"]["out"]
    actual_operating = direct["net_change"]-investing_net-financing_net
    other_adjustment = actual_operating-calculated_operating
    operating_net = calculated_operating+other_adjustment

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
        {"label":"التغير في العملاء والمدينين", "amount":-changes["receivables"], "kind":"working_capital"},
        {"label":"التغير في المخزون", "amount":-changes["inventory"], "kind":"working_capital"},
        {"label":"التغير في الأصول المتداولة الأخرى", "amount":-changes["other_current_assets"], "kind":"working_capital"},
        {"label":"التغير في الموردين والدائنين", "amount":changes["payables"], "kind":"working_capital"},
        {"label":"التغير في الالتزامات المتداولة الأخرى", "amount":changes["other_current_liabilities"], "kind":"working_capital"},
        {"label":"تسويات أخرى / بنود تحتاج تصنيفًا", "amount":other_adjustment, "kind":"reconciliation"},
    ]
    return {
        **direct, "method":"indirect", "sections":sections,
        "indirect_items":indirect_items,
        "indirect_operating_net":operating_net,
        "indirect_reconciliation":operating_net-actual_operating,
        "indirect_mapping_warning":not bool(current_asset_ids and current_liability_ids),
    }


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


__all__=["planning_bp","initialize_planning_schema","create_financial_year","list_financial_years","create_opening_entry","close_financial_year","reverse_financial_year_entry","build_balance_sheet_snapshot","build_cash_flow_snapshot","build_cash_flow_indirect_snapshot"]
