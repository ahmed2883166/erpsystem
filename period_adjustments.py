"""Controlled period-end accounting adjustments (prepaids, accruals, deferred items and provisions)."""
import sqlite3
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import wraps

from flask import Blueprint, abort, flash, redirect, render_template, request, session, url_for

from app_paths import DB_PATH
from database import connect_database
from accounting_core import post_journal

adjustments_bp = Blueprint("adjustments", __name__)
CENT = Decimal("0.01")
KINDS = {
    "prepaid_expense": ("مصروف مقدم", "expense", "asset"),
    "accrued_expense": ("مصروف مستحق", "expense", "liability"),
    "deferred_revenue": ("إيراد مقدم", "liability", "revenue"),
    "accrued_revenue": ("إيراد مستحق", "asset", "revenue"),
    "provision": ("مخصص", "expense", ("asset", "liability")),
}


def _db():
    return connect_database(DB_PATH)


def _money(value):
    try:
        result = Decimal(str(value or "0"))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("أدخل مبلغًا رقميًا صحيحًا.")
    if not result.is_finite() or result <= 0:
        raise ValueError("يجب أن يكون المبلغ أكبر من صفر.")
    return result.quantize(CENT, rounding=ROUND_HALF_UP)


def initialize_adjustments_schema(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS period_adjustments(
      id INTEGER PRIMARY KEY,
      adjustment_no TEXT NOT NULL UNIQUE,
      description TEXT NOT NULL,
      adjustment_type TEXT NOT NULL CHECK(adjustment_type IN
        ('prepaid_expense','accrued_expense','deferred_revenue','accrued_revenue','provision')),
      total_amount TEXT NOT NULL,
      start_date TEXT NOT NULL,
      end_date TEXT NOT NULL,
      frequency TEXT NOT NULL CHECK(frequency IN ('monthly','yearly')),
      debit_account_id INTEGER NOT NULL REFERENCES accounts(id),
      credit_account_id INTEGER NOT NULL REFERENCES accounts(id),
      status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','cancelled')),
      created_by INTEGER,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      CHECK(start_date<=end_date)
    );
    CREATE INDEX IF NOT EXISTS idx_period_adjustments_dates
      ON period_adjustments(status,start_date,end_date);
    CREATE TABLE IF NOT EXISTS period_adjustment_posts(
      id INTEGER PRIMARY KEY,
      adjustment_id INTEGER NOT NULL REFERENCES period_adjustments(id),
      period_start TEXT NOT NULL,
      period_end TEXT NOT NULL,
      amount TEXT NOT NULL,
      journal_entry_id INTEGER NOT NULL UNIQUE REFERENCES journal_entries(id),
      reversal_journal_entry_id INTEGER UNIQUE REFERENCES journal_entries(id),
      reversal_reason TEXT,
      created_by INTEGER,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      UNIQUE(adjustment_id,period_start,period_end)
    );
    CREATE INDEX IF NOT EXISTS idx_period_adjustment_posts_schedule
      ON period_adjustment_posts(adjustment_id,period_start,period_end);
    INSERT OR IGNORE INTO company_settings(setting_key,setting_value)
      VALUES('period_adjustment_frequency','monthly');
    """)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(period_adjustments)")}
    for name, declaration in (
        ("cancelled_by", "INTEGER"),
        ("cancelled_at", "TEXT"),
        ("cancellation_reason", "TEXT NOT NULL DEFAULT ''"),
    ):
        if name not in columns:
            conn.execute(f"ALTER TABLE period_adjustments ADD COLUMN {name} {declaration}")
    conn.commit()


def _periods(adjustment):
    start = date.fromisoformat(adjustment["start_date"])
    end = date.fromisoformat(adjustment["end_date"])
    frequency = adjustment["frequency"]
    result = []
    if frequency == "monthly":
        cursor = date(start.year, start.month, 1)
        while cursor <= end:
            next_month = date(cursor.year + (1 if cursor.month == 12 else 0),
                              1 if cursor.month == 12 else cursor.month + 1, 1)
            period_start = max(start, cursor)
            period_end = min(end, next_month - timedelta(days=1))
            result.append((period_start.isoformat(), period_end.isoformat()))
            cursor = next_month
    elif frequency == "yearly":
        cursor = start
        while cursor <= end:
            try:
                next_start = cursor.replace(year=cursor.year + 1)
            except ValueError:
                next_start = cursor.replace(year=cursor.year + 1, day=28)
            period_end = min(end, next_start - timedelta(days=1))
            result.append((cursor.isoformat(), period_end.isoformat()))
            cursor = next_start
    else:
        raise ValueError("دورية التسوية غير صحيحة.")
    total = Decimal(str(adjustment["total_amount"]))
    share = (total / Decimal(len(result))).quantize(CENT, rounding=ROUND_HALF_UP)
    amounts = [share] * len(result)
    amounts[-1] = total - sum(amounts[:-1], Decimal("0"))
    return [{"period_start": p[0], "period_end": p[1],
             "posting_date": p[1], "amount": amounts[i]}
            for i, p in enumerate(result)]


def _account(conn, account_id):
    row = conn.execute("""SELECT id,code,name_ar,account_type,is_group,active
        FROM accounts WHERE id=?""", (account_id,)).fetchone()
    if not row or row["is_group"] or not row["active"]:
        raise ValueError("اختر حسابًا تفصيليًا ونشطًا من دليل الحسابات.")
    return row


def _validate_accounts(conn, kind, debit_id, credit_id):
    if kind not in KINDS:
        raise ValueError("نوع التسوية غير صحيح.")
    _, debit_type, credit_type = KINDS[kind]
    debit = _account(conn, debit_id)
    credit = _account(conn, credit_id)
    if debit["account_type"] != debit_type:
        raise ValueError("نوع الحساب المدين لا يتوافق مع نوع التسوية.")
    allowed_credit = credit_type if isinstance(credit_type, tuple) else (credit_type,)
    if credit["account_type"] not in allowed_credit:
        raise ValueError("نوع الحساب الدائن لا يتوافق مع نوع التسوية.")
    if debit["id"] == credit["id"]:
        raise ValueError("لا يمكن استخدام الحساب نفسه في طرفي التسوية.")
    return debit, credit


def create_adjustment(conn, description, kind, total_amount, start_date, end_date,
                      frequency, debit_account_id, credit_account_id, created_by=None):
    from datetime import date as date_type
    description = str(description or "").strip()
    if not description or len(description) > 250:
        raise ValueError("اكتب وصفًا للتسوية لا يزيد عن 250 حرفًا.")
    try:
        start = date_type.fromisoformat(str(start_date)).isoformat()
        end = date_type.fromisoformat(str(end_date)).isoformat()
    except (TypeError, ValueError):
        raise ValueError("تواريخ التسوية غير صحيحة.")
    if end < start:
        raise ValueError("تاريخ نهاية التسوية يجب ألا يسبق تاريخ بدايتها.")
    if frequency not in {"monthly", "yearly"}:
        raise ValueError("اختر دورية شهرية أو سنوية.")
    amount = _money(total_amount)
    _validate_accounts(conn, kind, int(debit_account_id), int(credit_account_id))
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute("""INSERT INTO period_adjustments(
            adjustment_no,description,adjustment_type,total_amount,start_date,end_date,
            frequency,debit_account_id,credit_account_id,created_by)
            VALUES(?,?,?,?,?,?,?,?,?,?)""",
            ("ADJ-" + date.today().strftime("%Y%m%d") + "-" +
             str(conn.execute("SELECT COALESCE(MAX(id),0)+1 FROM period_adjustments").fetchone()[0]),
             description, kind, str(amount), start, end, frequency,
             int(debit_account_id), int(credit_account_id), created_by))
        conn.commit()
        return cur.lastrowid
    except Exception:
        conn.rollback()
        raise


def preview_adjustment(conn, adjustment_id):
    adjustment = conn.execute("SELECT * FROM period_adjustments WHERE id=?",
                              (adjustment_id,)).fetchone()
    if not adjustment:
        raise ValueError("التسوية غير موجودة.")
    posted = { (row["period_start"], row["period_end"]): row
        for row in conn.execute("""SELECT * FROM period_adjustment_posts
            WHERE adjustment_id=?""", (adjustment_id,)).fetchall() }
    periods = []
    for item in _periods(adjustment):
        existing = posted.get((item["period_start"], item["period_end"]))
        periods.append({**item, "status": "reversed" if existing and existing["reversal_journal_entry_id"]
                        else "posted" if existing else "pending",
                        "journal_entry_id": existing["journal_entry_id"] if existing else None,
                        "post_id": existing["id"] if existing else None,
                        "reversal_journal_entry_id": existing["reversal_journal_entry_id"] if existing else None})
    return {"adjustment": adjustment, "periods": periods}


def post_adjustment_period(conn, adjustment_id, period_start, period_end, created_by=None):
    conn.execute("BEGIN IMMEDIATE")
    try:
        preview = preview_adjustment(conn, adjustment_id)
        adjustment = preview["adjustment"]
        if adjustment["status"] != "active":
            raise ValueError("التسوية ملغاة ولا يمكن ترحيلها.")
        period = next((p for p in preview["periods"]
                       if p["period_start"] == period_start and p["period_end"] == period_end), None)
        if not period:
            raise ValueError("الفترة المختارة لا تتبع جدول التسوية.")
        if period["status"] != "pending":
            raise ValueError("تم ترحيل هذه الفترة من قبل؛ لا يمكن تكرار القيد.")
        debit, credit = _validate_accounts(conn, adjustment["adjustment_type"],
            adjustment["debit_account_id"], adjustment["credit_account_id"])
        amount = period["amount"]
        reference = f"{adjustment['adjustment_no']}-{period_start}"
        entry_id = post_journal(conn, period["posting_date"], reference,
            f"تسوية {adjustment['description']} — {period_start} إلى {period_end}",
            [(debit["id"], amount, 0, adjustment["description"]),
             (credit["id"], 0, amount, adjustment["description"])], created_by)
        conn.execute("""INSERT INTO period_adjustment_posts(
            adjustment_id,period_start,period_end,amount,journal_entry_id,created_by)
            VALUES(?,?,?,?,?,?)""",
            (adjustment_id, period_start, period_end, str(amount), entry_id, created_by))
        conn.commit()
        return entry_id
    except Exception:
        conn.rollback()
        raise


def reverse_adjustment_period(conn, post_id, reversal_date, reason, created_by=None):
    try:
        normalized_date = date.fromisoformat(str(reversal_date)).isoformat()
    except (TypeError, ValueError):
        raise ValueError("تاريخ عكس التسوية غير صحيح.")
    reason = str(reason or "").strip()
    if not reason or len(reason) > 500:
        raise ValueError("اكتب سببًا للعكس لا يزيد عن 500 حرف.")
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("""SELECT p.*,a.adjustment_no FROM period_adjustment_posts p
            JOIN period_adjustments a ON a.id=p.adjustment_id WHERE p.id=?""", (post_id,)).fetchone()
        if not row:
            raise ValueError("قيد التسوية غير موجود.")
        if row["reversal_journal_entry_id"]:
            raise ValueError("تم عكس قيد هذه الفترة بالفعل.")
        if normalized_date < row["period_end"]:
            raise ValueError("تاريخ عكس التسوية لا يمكن أن يسبق تاريخ القيد الأصلي.")
        original = conn.execute("SELECT status FROM journal_entries WHERE id=?",
                                (row["journal_entry_id"],)).fetchone()
        if not original or original["status"] != "posted":
            raise ValueError("القيد الأصلي غير موجود أو غير مرحّل.")
        lines = conn.execute("""SELECT account_id,debit,credit,memo FROM journal_lines
            WHERE entry_id=? ORDER BY id""", (row["journal_entry_id"],)).fetchall()
        if len(lines) < 2:
            raise ValueError("القيد الأصلي لا يحتوي على سطور كافية للعكس.")
        reversal_lines = [(line["account_id"], line["credit"], line["debit"],
                           "عكس تسوية: " + str(line["memo"] or "")) for line in lines]
        reversal_id = post_journal(conn, normalized_date,
            f"{row['adjustment_no']}-REV-{post_id}",
            f"عكس تسوية الفترة {row['period_start']} — {reason}",
            reversal_lines, created_by)
        conn.execute("""UPDATE period_adjustment_posts
            SET reversal_journal_entry_id=?,reversal_reason=? WHERE id=?""",
            (reversal_id, reason, post_id))
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
            if permission not in set(session.get("permissions", ())):
                abort(403)
            return fn(*args, **kwargs)
        return wrapped
    return decorate


@adjustments_bp.route("/period-adjustments", methods=["GET", "POST"])
@_require("reports.view")
def index():
    conn = _db()
    try:
        if request.method == "POST":
            if "journal.manage" not in set(session.get("permissions", ())):
                abort(403)
            if request.form.get("action") == "save_frequency":
                frequency = request.form.get("frequency")
                if frequency not in {"monthly", "yearly"}:
                    raise ValueError("اختر دورية شهرية أو سنوية.")
                conn.execute("""INSERT INTO company_settings(setting_key,setting_value)
                    VALUES('period_adjustment_frequency',?)
                    ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value""",
                    (frequency,))
                conn.commit()
                flash("تم حفظ دورية التسويات الافتراضية.", "success")
                return redirect(url_for("adjustments.index"))
            kind = request.form.get("adjustment_type")
            default_frequency = conn.execute("""SELECT setting_value FROM company_settings
                WHERE setting_key='period_adjustment_frequency'""").fetchone()
            frequency = request.form.get("frequency") or (default_frequency["setting_value"] if default_frequency else "monthly")
            adjustment_id = create_adjustment(conn, request.form.get("description"), kind,
                request.form.get("total_amount"), request.form.get("start_date"),
                request.form.get("end_date"), frequency,
                request.form.get("debit_account_id"), request.form.get("credit_account_id"),
                session.get("user_id"))
            flash(f"تم حفظ جدول التسوية رقم {adjustment_id}. لم يتم إنشاء أي قيد محاسبي؛ راجع المعاينة ثم رحّل كل فترة يدويًا.", "success")
            return redirect(url_for("adjustments.index", selected=adjustment_id))
        selected = request.args.get("selected", type=int)
        schedules = conn.execute("""SELECT a.*,d.name_ar debit_name,c.name_ar credit_name
            FROM period_adjustments a JOIN accounts d ON d.id=a.debit_account_id
            JOIN accounts c ON c.id=a.credit_account_id ORDER BY a.id DESC""").fetchall()
        preview = preview_adjustment(conn, selected) if selected else None
        accounts = conn.execute("""SELECT id,code,name_ar,account_type FROM accounts
            WHERE active=1 AND is_group=0 ORDER BY code""").fetchall()
        setting = conn.execute("""SELECT setting_value FROM company_settings
            WHERE setting_key='period_adjustment_frequency'""").fetchone()
        return render_template("period_adjustments.html", schedules=schedules,
            preview=preview, accounts=accounts,
            default_frequency=setting["setting_value"] if setting else "monthly",
            kinds=KINDS)
    except (ValueError, sqlite3.Error) as exc:
        conn.rollback()
        flash(str(exc) if isinstance(exc, ValueError) else "تعذر حفظ التسوية بسبب تعارض في البيانات.", "error")
        return redirect(url_for("adjustments.index"))
    finally:
        conn.close()


@adjustments_bp.route("/period-adjustments/<int:adjustment_id>/post", methods=["POST"])
@_require("journal.manage")
def post_period(adjustment_id):
    conn = _db()
    try:
        entry_id = post_adjustment_period(conn, adjustment_id,
            request.form.get("period_start", ""), request.form.get("period_end", ""),
            session.get("user_id"))
        flash(f"تم ترحيل قيد التسوية رقم {entry_id}.", "success")
    except (ValueError, sqlite3.Error) as exc:
        conn.rollback()
        flash(str(exc) if isinstance(exc, ValueError) else "تعذر ترحيل التسوية؛ لم يتم حفظ أي تغييرات.", "error")
    finally:
        conn.close()
    return redirect(url_for("adjustments.index", selected=adjustment_id))


@adjustments_bp.route("/period-adjustments/<int:adjustment_id>/cancel", methods=["POST"])
@_require("journal.manage")
def cancel_adjustment(adjustment_id):
    conn = _db()
    try:
        reason = str(request.form.get("reason") or "").strip()
        if not reason or len(reason) > 500:
            raise ValueError("اكتب سبب إلغاء جدول التسوية بما لا يزيد عن 500 حرف.")
        conn.execute("BEGIN IMMEDIATE")
        adjustment = conn.execute(
            "SELECT status FROM period_adjustments WHERE id=?", (adjustment_id,)
        ).fetchone()
        if not adjustment or adjustment["status"] != "active":
            raise ValueError("جدول التسوية غير موجود أو سبق إلغاؤه.")
        un_reversed = conn.execute("""SELECT COUNT(*) FROM period_adjustment_posts
            WHERE adjustment_id=? AND reversal_journal_entry_id IS NULL""",
            (adjustment_id,)).fetchone()[0]
        if un_reversed:
            raise ValueError("لا يمكن إلغاء الجدول قبل عكس كل الفترات المرحلة. اعكس القيود أولًا ثم ألغِ الجدول.")
        changed = conn.execute("""UPDATE period_adjustments SET status='cancelled',
            cancelled_by=?,cancelled_at=CURRENT_TIMESTAMP,cancellation_reason=?
            WHERE id=? AND status='active'""",
            (session.get("user_id"), reason, adjustment_id)).rowcount
        if changed != 1:
            raise ValueError("تغيرت حالة جدول التسوية أثناء الإلغاء.")
        conn.commit()
        flash("تم إلغاء جدول التسوية. لم يتم حذف أي قيد أو سجل.", "success")
    except (ValueError, sqlite3.Error) as exc:
        conn.rollback()
        flash(str(exc) if isinstance(exc, ValueError) else "تعذر إلغاء جدول التسوية.", "error")
    finally:
        conn.close()
    return redirect(url_for("adjustments.index"))


@adjustments_bp.route("/period-adjustments/posts/<int:post_id>/reverse", methods=["POST"])
@_require("journal.manage")
def reverse_period(post_id):
    conn = _db()
    try:
        entry_id = reverse_adjustment_period(conn, post_id,
            request.form.get("reversal_date", ""), request.form.get("reason", ""),
            session.get("user_id"))
        flash(f"تم إنشاء القيد العكسي رقم {entry_id} مع الاحتفاظ بالقيد الأصلي.", "success")
    except (ValueError, sqlite3.Error) as exc:
        conn.rollback()
        flash(str(exc) if isinstance(exc, ValueError) else "تعذر عكس التسوية؛ لم يتم حفظ أي تغييرات.", "error")
    finally:
        conn.close()
    return redirect(url_for("adjustments.index"))


__all__ = ["adjustments_bp", "initialize_adjustments_schema", "create_adjustment",
           "preview_adjustment", "post_adjustment_period", "reverse_adjustment_period"]
