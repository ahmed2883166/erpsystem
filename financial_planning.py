"""Budget planning and cash-flow reporting for NEXUS ERP."""
from app_paths import DB_PATH
from database import connect_database
import sqlite3
from accounting_core import post_journal
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


def create_opening_entry(conn, year_id, balances, created_by=None):
    """Post one balanced opening journal using balance-sheet accounts only."""
    _start_financial_year_transaction(conn)
    try:
        year = _financial_year_or_error(conn, year_id)
        if year["status"] != "open":
            raise ValueError("لا يمكن إنشاء قيد افتتاح لسنة غير مفتوحة.")
        if year["opening_entry_id"]:
            raise ValueError("تم إنشاء قيد افتتاح لهذه السنة بالفعل. صححه بعكس القيد أولًا.")
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


def close_financial_year(conn, year_id, created_by=None):
    """Close P&L balances into retained earnings and lock the financial year."""
    _start_financial_year_transaction(conn)
    try:
        year = _financial_year_or_error(conn, year_id)
        if year["status"] != "open":
            raise ValueError("السنة المالية ليست مفتوحة أو تم إقفالها بالفعل.")
        if year["closing_entry_id"]:
            raise ValueError("يوجد قيد إقفال مرتبط بهذه السنة بالفعل.")
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
            retained = conn.execute("""SELECT id FROM accounts
                WHERE code='3200' AND account_type='equity' AND is_group=0 AND active=1 LIMIT 1""").fetchone()
            if not retained:
                raise ValueError("حساب الأرباح المرحلة التفصيلي النشط (3200) غير موجود؛ راجع شجرة الحسابات قبل الإقفال.")
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
        conn.execute("UPDATE financial_years SET closing_entry_id=?,status='closed' WHERE id=?",
                     (entry_id, year_id))
        conn.execute("""INSERT INTO financial_year_journal_history
            (financial_year_id,entry_kind,action,entry_id,created_by)
            VALUES(?,'closing','created',?,?)""", (year_id, entry_id, created_by))
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
        entry_id = close_financial_year(conn, year_id, session.get("user_id"))
        flash(f"تم إقفال السنة المالية بقيد رقم {entry_id}.", "success")
    except (ValueError, sqlite3.IntegrityError) as exc:
        flash(str(exc) or "تعذر إقفال السنة المالية.", "error")
    finally:
        conn.close()
    return redirect(url_for("planning.financial_years"))


@planning_bp.route("/financial-years/<int:year_id>/reverse/<entry_kind>", methods=["POST"])
@_require("journal.manage")
def financial_year_reverse(year_id, entry_kind):
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


@planning_bp.route("/reports/cash-flow")
@_require("cashflow.view")
def cash_flow():
    from datetime import date
    raw_from=request.args.get("date_from","").strip()
    raw_to=request.args.get("date_to","").strip()
    today=date.today()
    try:
        start=date.fromisoformat(raw_from) if raw_from else today.replace(day=1)
        end=date.fromisoformat(raw_to) if raw_to else today
        if end<start: raise ValueError()
    except ValueError:
        flash("فترة التقرير غير صحيحة.","error")
        start,end=today.replace(day=1),today
    conn=_connect()
    try:
        role_rows=conn.execute("SELECT account_id FROM management_account_roles WHERE role='cash'").fetchall()
        cash_ids={r["account_id"] for r in role_rows}
        custom_mapping=conn.execute("SELECT meta_value FROM management_dashboard_meta WHERE meta_key='roles_customized'").fetchone()
        if not cash_ids and not (custom_mapping and custom_mapping["meta_value"]=="1"):
            cash_ids={r["id"] for r in conn.execute(
              "SELECT id FROM accounts WHERE is_group=0 AND (code IN ('1110','1120') OR name_ar LIKE '%صندوق%' OR name_ar LIKE '%بنك%')")}
        if not cash_ids:
            flash("حدد حسابات الصندوق والبنوك من شاشة ربط حسابات لوحة الإدارة أولًا.","warning")
            opening=closing=Decimal("0"); sections={k:{"in":Decimal("0"),"out":Decimal("0")} for k in ("operating","investing","financing")}
        else:
            marks=",".join("?" for _ in cash_ids)
            ids=tuple(sorted(cash_ids))
            opening_row=conn.execute(f"""SELECT COALESCE(DECIMAL_NET(jl.debit,jl.credit),0) value
              FROM journal_lines jl JOIN journal_entries je ON je.id=jl.entry_id
              WHERE jl.account_id IN ({marks}) AND je.status='posted' AND je.entry_date<?""",(*ids,start.isoformat())).fetchone()
            opening=Decimal(str(opening_row["value"] or "0"))
            sections={k:{"in":Decimal("0"),"out":Decimal("0")} for k in ("operating","investing","financing")}
            entries=conn.execute(f"""SELECT je.id,
                COALESCE(DECIMAL_NET(CASE WHEN jl.account_id IN ({marks}) THEN jl.debit ELSE '0' END,
                                     CASE WHEN jl.account_id IN ({marks}) THEN jl.credit ELSE '0' END),0) cash_net
              FROM journal_entries je JOIN journal_lines jl ON jl.entry_id=je.id
              WHERE je.status='posted' AND je.entry_date>=? AND je.entry_date<=?
              GROUP BY je.id ORDER BY je.entry_date,je.id""",(*ids,*ids,start.isoformat(),end.isoformat())).fetchall()
            for entry in entries:
                net=Decimal(str(entry["cash_net"] or "0"))
                if not net: continue
                counterpart=conn.execute(f"""SELECT a.account_type,a.code,a.name_ar,jl.debit,jl.credit
                    FROM journal_lines jl JOIN accounts a ON a.id=jl.account_id
                    WHERE jl.entry_id=? AND jl.account_id NOT IN ({marks})""",(entry["id"],*ids)).fetchall()
                weights={"operating":Decimal("0"),"investing":Decimal("0"),"financing":Decimal("0")}
                for item in counterpart:
                    code=item["code"]; name=item["name_ar"]
                    if item["account_type"]=="asset" and not (code.startswith(("11","21")) or "عميل" in name or "مخزون" in name or "مقدم" in name):
                        category="investing"
                    elif item["account_type"] == "equity" or (item["account_type"] == "liability" and (code.startswith("22") or "قرض" in name)):
                        category="financing"
                    else:
                        category="operating"
                    weights[category]+=abs(Decimal(str(item["debit"] or "0")))+abs(Decimal(str(item["credit"] or "0")))
                total_weight=sum(weights.values(),Decimal("0"))
                if not total_weight: weights["operating"]=Decimal("1"); total_weight=Decimal("1")
                remaining=abs(net)
                active=[k for k,v in weights.items() if v]
                for category in active[:-1]:
                    part=(abs(net)*weights[category]/total_weight).quantize(CENT,rounding=ROUND_HALF_UP)
                    remaining-=part
                    if net>0: sections[category]["in"]+=part
                    else: sections[category]["out"]+=part
                category=active[-1]
                if net>0: sections[category]["in"]+=remaining
                else: sections[category]["out"]+=remaining
            movement=sum((x["in"]-x["out"] for x in sections.values()),Decimal("0"))
            closing=opening+movement
    finally:
        conn.close()
    total_in=sum((x["in"] for x in sections.values()),Decimal("0"))
    total_out=sum((x["out"] for x in sections.values()),Decimal("0"))
    net_change=closing-opening
    reconciliation_difference=net_change-(total_in-total_out)
    return render_template("cash_flow.html",date_from=start.isoformat(),date_to=end.isoformat(),
        opening=opening,closing=closing,sections=sections,total_in=total_in,total_out=total_out,
        net_change=net_change,reconciliation_difference=reconciliation_difference,
        cash_mapping_available=bool(cash_ids),
        cash_reconciles=(bool(cash_ids) and reconciliation_difference==0))


__all__=["planning_bp","initialize_planning_schema","create_financial_year","list_financial_years","create_opening_entry","close_financial_year","reverse_financial_year_entry"]
