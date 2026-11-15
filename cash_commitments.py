"""Additive register for cash commitments not already tracked by operational subledgers.

Payments link to existing posted journal entries. This module never posts GL entries.
"""
from datetime import date
import calendar
from decimal import Decimal, InvalidOperation

COMMITMENT_TYPES = {"tax", "recurring_expense", "friday_overtime"}


def _amount(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("قيمة المبلغ غير صحيحة.")
    if not result.is_finite() or result <= 0:
        raise ValueError("يجب أن يكون المبلغ أكبر من صفر.")
    return result.quantize(Decimal("0.01"))



def _validate_payable_account(conn, account_id, required=False):
    if account_id in (None, ""):
        if required:
            raise ValueError("اختر حساب الالتزام المستحق.")
        return None
    row = conn.execute(
        "SELECT account_type,is_group,active FROM accounts WHERE id=?",
        (int(account_id),),
    ).fetchone()
    if not row or not row["active"] or row["is_group"] or row["account_type"] != "liability":
        raise ValueError("حساب الالتزام يجب أن يكون حساب خصوم تفصيليًا ونشطًا.")
    return int(account_id)


def initialize_cash_commitments_schema(conn):
    """Idempotent additive migration; does not alter existing business tables."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS cash_commitments (
            id INTEGER PRIMARY KEY,
            commitment_type TEXT NOT NULL
                CHECK(commitment_type IN ('tax','recurring_expense','friday_overtime')),
            title TEXT NOT NULL,
            period_label TEXT NOT NULL DEFAULT '',
            amount TEXT NOT NULL,
            due_date TEXT NOT NULL,
            currency_code TEXT NOT NULL DEFAULT '',
            payable_account_id INTEGER,
            origin_type TEXT NOT NULL DEFAULT '',
            origin_id INTEGER,
            status TEXT NOT NULL DEFAULT 'open'
                CHECK(status IN ('open','partially_paid','paid','void')),
            notes TEXT NOT NULL DEFAULT '',
            created_by INTEGER,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(origin_type, origin_id)
        );
        CREATE INDEX IF NOT EXISTS idx_cash_commitments_due_status
            ON cash_commitments(status,due_date,id);
        CREATE TABLE IF NOT EXISTS cash_commitment_payments (
            id INTEGER PRIMARY KEY,
            commitment_id INTEGER NOT NULL
                REFERENCES cash_commitments(id) ON DELETE RESTRICT,
            payment_date TEXT NOT NULL,
            amount TEXT NOT NULL,
            journal_entry_id INTEGER NOT NULL UNIQUE
                REFERENCES journal_entries(id) ON DELETE RESTRICT,
            reversal_journal_entry_id INTEGER UNIQUE
                REFERENCES journal_entries(id) ON DELETE RESTRICT,
            reference TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '',
            created_by INTEGER,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_cash_commitment_payments_commitment
            ON cash_commitment_payments(commitment_id,payment_date,id);
        CREATE TABLE IF NOT EXISTS cash_commitment_schedules (
            id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            amount TEXT NOT NULL,
            currency_code TEXT NOT NULL DEFAULT '',
            frequency TEXT NOT NULL CHECK(frequency IN ('monthly','quarterly','semiannual','annual')),
            interval_months INTEGER NOT NULL CHECK(interval_months IN (1,3,6,12)),
            anchor_day INTEGER NOT NULL CHECK(anchor_day BETWEEN 1 AND 31),
            next_due_date TEXT NOT NULL,
            end_date TEXT,
            payable_account_id INTEGER,
            active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
            notes TEXT NOT NULL DEFAULT '',
            created_by INTEGER,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS cash_commitment_schedule_occurrences (
            id INTEGER PRIMARY KEY,
            schedule_id INTEGER NOT NULL REFERENCES cash_commitment_schedules(id) ON DELETE RESTRICT,
            due_date TEXT NOT NULL,
            commitment_id INTEGER UNIQUE REFERENCES cash_commitments(id) ON DELETE RESTRICT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(schedule_id,due_date)
        );
        CREATE INDEX IF NOT EXISTS idx_cash_commitment_schedules_active
            ON cash_commitment_schedules(active,next_due_date);
    """)
    payment_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(cash_commitment_payments)")
    }
    if "reversal_journal_entry_id" not in payment_columns:
        conn.execute(
            "ALTER TABLE cash_commitment_payments ADD COLUMN reversal_journal_entry_id "
            "INTEGER REFERENCES journal_entries(id) ON DELETE RESTRICT"
        )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_cash_commitment_payment_reversal "
        "ON cash_commitment_payments(reversal_journal_entry_id)"
    )
    conn.commit()


def create_cash_commitment(conn, commitment_type, title, amount, due_date,
                           period_label="", currency_code="", payable_account_id=None,
                           origin_type="", origin_id=None, notes="", created_by=None):
    if commitment_type not in COMMITMENT_TYPES:
        raise ValueError("نوع الالتزام غير صحيح.")
    title = str(title or "").strip()
    if not title:
        raise ValueError("اسم الالتزام مطلوب.")
    amount = _amount(amount)
    due_date = date.fromisoformat(str(due_date)).isoformat()
    payable_account_id = _validate_payable_account(conn,payable_account_id,required=True)
    origin_type = str(origin_type or "").strip()
    if bool(origin_type) != (origin_id is not None):
        raise ValueError("مرجع المصدر يجب أن يحتوي النوع والمعرف معًا.")
    cur = conn.execute(
        """INSERT INTO cash_commitments
           (commitment_type,title,period_label,amount,due_date,currency_code,
            payable_account_id,origin_type,origin_id,notes,created_by)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (commitment_type,title,str(period_label or "").strip(),str(amount),due_date,
         str(currency_code or "").strip(),payable_account_id,origin_type,origin_id,
         str(notes or "").strip(),created_by),
    )
    conn.commit()
    return cur.lastrowid


def _journal_entry_effect_is_active(conn, journal_entry_id):
    """Whether a posted journal still has its original accounting effect."""
    entry = conn.execute(
        "SELECT status,reference,description FROM journal_entries WHERE id=?",
        (journal_entry_id,),
    ).fetchone()
    if not entry or entry["status"] != "posted":
        return False
    reference = str(entry["reference"] or "")
    description = str(entry["description"] or "")
    if reference.startswith("REV-") or description.startswith("قيد عكسي للقيد رقم "):
        return False
    reversal = conn.execute(
        """SELECT id FROM journal_entries
           WHERE status='posted' AND
             (reference=? OR description LIKE ?)
           ORDER BY id LIMIT 1""",
        (f"REV-{journal_entry_id}", f"قيد عكسي للقيد رقم {journal_entry_id}:%"),
    ).fetchone()
    if not reversal:
        return True
    # The supported reversal-of-reversal restores the original accounting effect.
    restored = conn.execute(
        """SELECT 1 FROM journal_entries
           WHERE status='posted' AND
             (reference=? OR description LIKE ?)
           LIMIT 1""",
        (f"REV-{int(reversal['id'])}", f"قيد عكسي للقيد رقم {int(reversal['id'])}:%"),
    ).fetchone()
    return restored is not None


def cash_commitment_payment_summary(conn, commitment_id):
    commitment = conn.execute(
        "SELECT amount,status FROM cash_commitments WHERE id=?", (commitment_id,)
    ).fetchone()
    if not commitment:
        raise ValueError("الالتزام غير موجود.")
    payments = conn.execute(
        """SELECT p.*,j.status journal_status,j.reference journal_reference,
                  j.description journal_description
           FROM cash_commitment_payments p
           LEFT JOIN journal_entries j ON j.id=p.journal_entry_id
           WHERE p.commitment_id=? ORDER BY p.payment_date,p.id""",
        (commitment_id,),
    ).fetchall()
    effective = Decimal("0")
    details = []
    for payment in payments:
        active = (
            payment["reversal_journal_entry_id"] is None
            and _journal_entry_effect_is_active(conn, payment["journal_entry_id"])
        )
        if active:
            effective += Decimal(str(payment["amount"]))
        details.append({
            **dict(payment),
            "effective": active,
        })
    total = Decimal(str(commitment["amount"]))
    status = commitment["status"]
    if status != "void":
        status = "paid" if effective >= total else (
            "partially_paid" if effective > 0 else "open"
        )
    return {"paid_total": effective, "status": status, "payments": details}


def record_cash_commitment_payment(conn, commitment_id, payment_date, amount,
                                   journal_entry_id, reference="", notes="",
                                   created_by=None, payable_account_id=None):
    payment_date = date.fromisoformat(str(payment_date)).isoformat()
    amount = _amount(amount)
    conn.execute("BEGIN IMMEDIATE")
    try:
        commitment = conn.execute(
            "SELECT id,amount,status,payable_account_id FROM cash_commitments WHERE id=?", (commitment_id,)
        ).fetchone()
        if not commitment or commitment["status"] == "void":
            raise ValueError("الالتزام غير موجود أو ملغي.")
        journal = conn.execute(
            "SELECT id,reference,description FROM journal_entries WHERE id=? AND status='posted'",
            (journal_entry_id,),
        ).fetchone()
        if not journal:
            raise ValueError("يجب ربط الدفعة بقيد يومية مرحّل موجود.")
        if (str(journal["reference"] or "").startswith("REV-")
                or str(journal["description"] or "").startswith("قيد عكسي للقيد رقم ")):
            raise ValueError("لا يمكن ربط قيد عكسي كسند سداد.")
        if not _journal_entry_effect_is_active(conn, journal_entry_id):
            raise ValueError("قيد السداد تم عكسه؛ لا يمكن ربطه بالتزام.")
        selected_payable_account = _validate_payable_account(
            conn,commitment["payable_account_id"] or payable_account_id,required=True
        )
        if not selected_payable_account:
            raise ValueError("حدد حساب الالتزام المستحق قبل ربط دفعة السداد.")
        if (commitment["payable_account_id"] is not None
                and payable_account_id is not None
                and int(commitment["payable_account_id"]) != int(payable_account_id)):
            raise ValueError("حساب الالتزام لا يطابق الحساب المحدد.")
        debit_rows = conn.execute(
            """SELECT debit FROM journal_lines
               WHERE entry_id=? AND account_id=?""",
            (journal_entry_id,selected_payable_account),
        ).fetchall()
        payable_debit = sum((Decimal(str(row["debit"])) for row in debit_rows), Decimal("0"))
        if payable_debit != amount:
            raise ValueError("قيمة مدين حساب الالتزام في قيد السداد يجب أن تساوي مبلغ الدفعة تمامًا.")
        if commitment["payable_account_id"] is None:
            conn.execute(
                "UPDATE cash_commitments SET payable_account_id=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (selected_payable_account,commitment_id),
            )
        paid = cash_commitment_payment_summary(conn, commitment_id)["paid_total"]
        total = Decimal(str(commitment["amount"]))
        if paid + amount > total:
            raise ValueError("قيمة الدفعة تتجاوز الرصيد المتبقي من الالتزام.")
        conn.execute(
            """INSERT INTO cash_commitment_payments
               (commitment_id,payment_date,amount,journal_entry_id,reference,notes,created_by)
               VALUES(?,?,?,?,?,?,?)""",
            (commitment_id,payment_date,str(amount),journal_entry_id,
             str(reference or "").strip(),str(notes or "").strip(),created_by),
        )
        status = "paid" if paid + amount == total else "partially_paid"
        conn.execute(
            "UPDATE cash_commitments SET status=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (status,commitment_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def reverse_cash_commitment_payment(conn, payment_id, reversal_date,
                                      reason, created_by=None, post_journal_func=None):
    """Reverse a linked payment through this register, preserving journal history."""
    reversal_date = date.fromisoformat(str(reversal_date)).isoformat()
    reason = str(reason or "").strip()
    if not reason or len(reason) > 500:
        raise ValueError("اكتب سببًا لعكس السداد (بحد أقصى 500 حرف).")
    conn.execute("BEGIN IMMEDIATE")
    try:
        payment = conn.execute(
            """SELECT p.*,c.id commitment_id,c.amount commitment_amount,c.status commitment_status,
                      c.title commitment_title,c.payable_account_id
               FROM cash_commitment_payments p
               JOIN cash_commitments c ON c.id=p.commitment_id
               WHERE p.id=?""",
            (payment_id,),
        ).fetchone()
        if not payment:
            raise ValueError("دفعة الالتزام غير موجودة.")
        if payment["commitment_status"] == "void":
            raise ValueError("لا يمكن عكس دفعة مرتبطة بالتزام ملغي.")
        if payment["reversal_journal_entry_id"] is not None:
            raise ValueError("تم عكس هذه الدفعة بالفعل.")
        if not _journal_entry_effect_is_active(conn, payment["journal_entry_id"]):
            raise ValueError("قيد السداد غير مرحّل أو سبق عكسه.")
        lines = conn.execute(
            """SELECT account_id,debit,credit,memo FROM journal_lines
               WHERE entry_id=? ORDER BY id""",
            (payment["journal_entry_id"],),
        ).fetchall()
        if len(lines) < 2:
            raise ValueError("قيد السداد الأصلي لا يحتوي على سطور كافية للعكس.")
        payable_debit = sum((
            Decimal(str(line["debit"] or "0"))
            for line in lines
            if int(line["account_id"]) == int(payment["payable_account_id"] or 0)
        ), Decimal("0"))
        if payable_debit != Decimal(str(payment["amount"])):
            raise ValueError(
                "لا يمكن عكس السداد آليًا: مدين حساب الالتزام في القيد لا يساوي مبلغ الدفعة المسجلة. "
                "يلزم مراجعة الربط المحاسبي أولًا."
            )
        reverse_lines = [
            (line["account_id"], line["credit"], line["debit"],
             "عكس سداد التزام: " + str(line["memo"] or ""))
            for line in lines
        ]
        if post_journal_func is None:
            from accounting_core import post_journal
            post_journal_func = post_journal
        reversal_id = post_journal_func(
            conn, reversal_date, f"REV-{payment['journal_entry_id']}",
            f"عكس سداد الالتزام {payment['commitment_title']} — السبب: {reason}",
            reverse_lines, created_by,
        )
        conn.execute(
            """UPDATE cash_commitment_payments
               SET reversal_journal_entry_id=? WHERE id=? AND reversal_journal_entry_id IS NULL""",
            (reversal_id, payment_id),
        )
        summary = cash_commitment_payment_summary(conn, payment["commitment_id"])
        conn.execute(
            """UPDATE cash_commitments SET status=?,updated_at=CURRENT_TIMESTAMP
               WHERE id=? AND status!='void'""",
            (summary["status"], payment["commitment_id"]),
        )
        conn.commit()
        return reversal_id
    except Exception:
        conn.rollback()
        raise


def void_cash_commitment(conn, commitment_id):
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT status FROM cash_commitments WHERE id=?", (commitment_id,)
        ).fetchone()
        if not row:
            raise ValueError("الالتزام غير موجود.")
        summary = cash_commitment_payment_summary(conn, commitment_id)
        if summary["paid_total"] > 0 or row["status"] == "void":
            raise ValueError("لا يمكن إلغاء التزام عليه دفعات فعالة أو تم إلغاؤه بالفعل.")
        conn.execute(
            "UPDATE cash_commitments SET status='void',updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (commitment_id,),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def list_cash_commitments_forecast(conn, as_of, end_date):
    """Return unpaid commitment balances due within the forecast horizon."""
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='cash_commitments'"
    ).fetchone()
    if not exists:
        return []
    rows = conn.execute(
        """SELECT c.* FROM cash_commitments c
           WHERE c.status!='void'
             AND c.due_date<=?
           ORDER BY c.due_date,c.id""",
        (end_date,),
    ).fetchall()
    items = []
    for row in rows:
        payment_rows = conn.execute(
            """SELECT p.amount,p.journal_entry_id,p.reversal_journal_entry_id
               FROM cash_commitment_payments p
               WHERE p.commitment_id=? AND p.payment_date<=?""",
            (row["id"],as_of),
        ).fetchall()
        paid = sum((
            Decimal(str(payment["amount"]))
            for payment in payment_rows
            if payment["reversal_journal_entry_id"] is None
            and _journal_entry_effect_is_active(conn, payment["journal_entry_id"])
        ), Decimal("0"))
        remaining = Decimal(str(row["amount"])) - paid
        if remaining <= 0:
            continue
        items.append({
            "kind":"cash_commitment",
            "commitment_type":row["commitment_type"],
            "party":"التزامات أخرى",
            "document":row["title"],
            "due_date":row["due_date"],
            "balance":remaining,
            "status":"متأخر" if row["due_date"] < as_of else "مستحق/متوقع",
        })
    return items



def sync_friday_allowance_commitments(conn, created_by=None):
    """Import legacy posted Friday batches without touching their journals or source rows.

    A posted reversal cancels the batch accrual. If that reversal itself has a
    posted reversal, the original accounting effect is restored and import is
    allowed. The journal reversal workflow permits exactly this two-step chain.
    """
    def table_exists(name):
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone() is not None

    required_tables = {"friday_allowance_batches", "journal_entries", "cash_commitments"}
    missing = sorted(name for name in required_tables if not table_exists(name))
    if missing:
        raise ValueError("تعذر مراجعة الكشوف؛ جداول مطلوبة غير موجودة: " + "، ".join(missing))

    settings_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(company_settings)")
    } if table_exists("company_settings") else set()
    currency_code = ""
    if {"setting_key", "setting_value"}.issubset(settings_columns):
        setting = conn.execute(
            "SELECT setting_value FROM company_settings WHERE setting_key='base_currency' LIMIT 1"
        ).fetchone()
        currency_code = str(setting["setting_value"] or "") if setting else ""

    owns_transaction = not conn.in_transaction
    savepoint = "sync_friday_allowance_commitments"
    if owns_transaction:
        conn.execute("BEGIN IMMEDIATE")
    else:
        conn.execute(f"SAVEPOINT {savepoint}")
    imported = 0
    skipped = []
    try:
        batches = conn.execute(
            """SELECT id,period,status,total_amount,payable_account_id,journal_entry_id
               FROM friday_allowance_batches
               WHERE status='posted' AND journal_entry_id IS NOT NULL
               ORDER BY period,id"""
        ).fetchall()
        for batch in batches:
            batch_id = int(batch["id"])
            if conn.execute(
                """SELECT 1 FROM cash_commitments
                   WHERE origin_type='friday_allowance_batch' AND origin_id=? LIMIT 1""",
                (batch_id,),
            ).fetchone():
                skipped.append({"batch_id": batch_id, "reason": "مرتبط بالفعل"})
                continue

            journal_id = int(batch["journal_entry_id"])
            original = conn.execute(
                "SELECT id,status FROM journal_entries WHERE id=?", (journal_id,)
            ).fetchone()
            if not original or original["status"] != "posted":
                skipped.append({"batch_id": batch_id, "reason": "قيد الاستحقاق غير موجود أو غير مرحّل"})
                continue

            reversal = conn.execute(
                """SELECT id FROM journal_entries
                   WHERE reference=? AND status='posted' ORDER BY id LIMIT 1""",
                (f"REV-{journal_id}",),
            ).fetchone()
            if reversal:
                restored = conn.execute(
                    """SELECT 1 FROM journal_entries
                       WHERE reference=? AND status='posted' LIMIT 1""",
                    (f"REV-{int(reversal['id'])}",),
                ).fetchone()
                if not restored:
                    skipped.append({"batch_id": batch_id, "reason": "قيد الاستحقاق معكوس"})
                    continue

            try:
                period = str(batch["period"] or "")
                if len(period) != 7 or period[4] != "-":
                    raise ValueError("فترة الكشف غير صحيحة")
                first_day = date.fromisoformat(period + "-01")
                due_date = date(first_day.year, first_day.month,
                                calendar.monthrange(first_day.year, first_day.month)[1]).isoformat()
                amount = _amount(batch["total_amount"])
                payable_account_id = _validate_payable_account(
                    conn, batch["payable_account_id"], required=True
                )
            except (ValueError, TypeError, InvalidOperation) as exc:
                skipped.append({"batch_id": batch_id, "reason": str(exc)})
                continue

            conn.execute(
                """INSERT INTO cash_commitments
                   (commitment_type,title,period_label,amount,due_date,currency_code,
                    payable_account_id,origin_type,origin_id,notes,created_by)
                   VALUES('friday_overtime',?,?,?,?,?,?,
                          'friday_allowance_batch',?,'استيراد كشف سابق بعد المراجعة',?)""",
                ("بدل الجمعة - " + period, period, str(amount), due_date,
                 currency_code, payable_account_id, batch_id, created_by),
            )
            imported += 1
        if owns_transaction:
            conn.commit()
        else:
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        return {"imported": imported, "skipped": skipped}
    except Exception:
        if owns_transaction:
            conn.rollback()
        else:
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise

_FREQUENCY_MONTHS = {
    "monthly": 1,
    "quarterly": 3,
    "semiannual": 6,
    "annual": 12,
}


def _advance_schedule_date(current_date, interval_months, anchor_day):
    current = date.fromisoformat(str(current_date))
    month_index = current.year * 12 + (current.month - 1) + int(interval_months)
    year, month_zero = divmod(month_index, 12)
    month = month_zero + 1
    day = min(int(anchor_day), calendar.monthrange(year, month)[1])
    return date(year, month, day).isoformat()


def create_recurring_schedule(conn, title, amount, first_due_date, frequency,
                              currency_code="", end_date=None, payable_account_id=None,
                              notes="", created_by=None):
    title = str(title or "").strip()
    if not title:
        raise ValueError("اسم المصروف الدوري مطلوب.")
    amount = _amount(amount)
    first_due_date = date.fromisoformat(str(first_due_date)).isoformat()
    payable_account_id = _validate_payable_account(conn,payable_account_id,required=True)
    interval_months = _FREQUENCY_MONTHS.get(str(frequency))
    if not interval_months:
        raise ValueError("دورية المصروف غير صحيحة.")
    normalized_end = date.fromisoformat(str(end_date)).isoformat() if end_date else None
    if normalized_end and normalized_end < first_due_date:
        raise ValueError("تاريخ نهاية التكرار يسبق أول استحقاق.")
    cursor = conn.execute(
        """INSERT INTO cash_commitment_schedules
           (title,amount,currency_code,frequency,interval_months,anchor_day,
            next_due_date,end_date,payable_account_id,notes,created_by)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (title,str(amount),str(currency_code or "").strip(),str(frequency),
         interval_months,date.fromisoformat(first_due_date).day,first_due_date,
         normalized_end,payable_account_id,str(notes or "").strip(),created_by),
    )
    conn.commit()
    return cursor.lastrowid


def generate_recurring_schedule_occurrences(conn, schedule_id, through_date):
    """Explicitly materialize scheduled expense occurrences; safe to repeat."""
    through_date = date.fromisoformat(str(through_date)).isoformat()
    conn.execute("BEGIN IMMEDIATE")
    try:
        schedule = conn.execute(
            "SELECT * FROM cash_commitment_schedules WHERE id=?", (schedule_id,)
        ).fetchone()
        if not schedule:
            raise ValueError("جدول المصروف الدوري غير موجود.")
        if not schedule["active"]:
            raise ValueError("جدول المصروف الدوري متوقف.")
        next_due = schedule["next_due_date"]
        if schedule["end_date"] and next_due > schedule["end_date"]:
            conn.execute(
                "UPDATE cash_commitment_schedules SET active=0,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (schedule_id,),
            )
            conn.commit()
            return 0
        generated = 0
        while next_due <= through_date and (
            not schedule["end_date"] or next_due <= schedule["end_date"]
        ):
            existing = conn.execute(
                """SELECT id FROM cash_commitment_schedule_occurrences
                   WHERE schedule_id=? AND due_date=?""",
                (schedule_id,next_due),
            ).fetchone()
            if not existing:
                occurrence = conn.execute(
                    """INSERT INTO cash_commitment_schedule_occurrences
                       (schedule_id,due_date) VALUES(?,?)""",
                    (schedule_id,next_due),
                )
                occurrence_id = occurrence.lastrowid
                commitment = conn.execute(
                    """INSERT INTO cash_commitments
                       (commitment_type,title,period_label,amount,due_date,currency_code,
                        payable_account_id,origin_type,origin_id,notes,created_by)
                       VALUES('recurring_expense',?,?,?,?,?,?, 'recurring_schedule_occurrence',?,?,?)""",
                    (schedule["title"],next_due[:7],schedule["amount"],next_due,
                     schedule["currency_code"],schedule["payable_account_id"],
                     occurrence_id,schedule["notes"],schedule["created_by"]),
                )
                conn.execute(
                    "UPDATE cash_commitment_schedule_occurrences SET commitment_id=? WHERE id=?",
                    (commitment.lastrowid,occurrence_id),
                )
                generated += 1
            next_due = _advance_schedule_date(
                next_due,schedule["interval_months"],schedule["anchor_day"]
            )
        conn.execute(
            """UPDATE cash_commitment_schedules
               SET next_due_date=?,active=CASE WHEN end_date IS NOT NULL AND ? > end_date
                    THEN 0 ELSE active END,updated_at=CURRENT_TIMESTAMP
               WHERE id=?""",
            (next_due,next_due,schedule_id),
        )
        conn.commit()
        return generated
    except Exception:
        conn.rollback()
        raise


def deactivate_recurring_schedule(conn, schedule_id):
    cursor = conn.execute(
        """UPDATE cash_commitment_schedules SET active=0,updated_at=CURRENT_TIMESTAMP
           WHERE id=? AND active=1""",
        (schedule_id,),
    )
    conn.commit()
    if not cursor.rowcount:
        raise ValueError("جدول المصروف الدوري غير موجود أو متوقف بالفعل.")
