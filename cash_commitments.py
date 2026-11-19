"""Additive register for cash commitments not already tracked by operational subledgers.

Payments link to existing posted journal entries. This module never posts GL entries.
"""
from datetime import date
import calendar
import re

from db_insert import insert_one_returning_id
from db_compat import (
    table_columns, begin_write_transaction, transaction_is_active, transactional_migration,
    table_exists as db_table_exists, execute_script, database_dialect,
)
from db_sql import execute_compat
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
    row = execute_compat(conn, 
        "SELECT account_type,is_group,active FROM accounts WHERE id=?",
        (int(account_id),),
    ).fetchone()
    if not row or not row["active"] or row["is_group"] or row["account_type"] != "liability":
        raise ValueError("حساب الالتزام يجب أن يكون حساب خصوم تفصيليًا ونشطًا.")
    return int(account_id)


@transactional_migration
def initialize_cash_commitments_schema(conn):
    """Idempotent additive migration; does not alter existing business tables."""
    schema_sql = """
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
        CREATE TABLE IF NOT EXISTS cash_commitment_duplicate_reviews (
            id INTEGER PRIMARY KEY, origin_type TEXT NOT NULL, origin_id INTEGER NOT NULL,
            kept_commitment_id INTEGER NOT NULL REFERENCES cash_commitments(id),
            voided_commitment_id INTEGER NOT NULL REFERENCES cash_commitments(id),
            reason TEXT NOT NULL, reviewed_by INTEGER,
            reviewed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(voided_commitment_id)
        );
        CREATE INDEX IF NOT EXISTS idx_cash_commitment_schedules_active
            ON cash_commitment_schedules(active,next_due_date);
    """
    if database_dialect(conn) == "postgresql":
        # SQLite assigns rowids for INTEGER PRIMARY KEY; PostgreSQL uses identity.
        schema_sql = re.sub(
            r"\bid INTEGER PRIMARY KEY\b",
            "id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY",
            schema_sql,
        )
    execute_script(conn, schema_sql)
    commitment_columns = table_columns(conn, "cash_commitments")
    if "origin_type" not in commitment_columns:
        execute_compat(
            conn,
            "ALTER TABLE cash_commitments ADD COLUMN origin_type TEXT NOT NULL DEFAULT ''"
        )
    if "origin_id" not in commitment_columns:
        execute_compat(
            conn,
            "ALTER TABLE cash_commitments ADD COLUMN origin_id INTEGER"
        )

    # Protect imports/retries from duplicate source-linked obligations. The
    # index is added after table creation and existing duplicates fail closed.
    duplicate_origins = execute_compat(conn, """
        SELECT origin_type,origin_id,COUNT(*) AS n
        FROM cash_commitments
        WHERE origin_type IN ('friday_allowance_batch','recurring_schedule_occurrence')
          AND origin_id IS NOT NULL
        GROUP BY origin_type,origin_id HAVING COUNT(*)>1
        LIMIT 10
    """).fetchall()
    # Legacy duplicates must not prevent startup; leave them visible for review.
    if not duplicate_origins:
        execute_compat(conn, """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_cash_commitments_source_origin
            ON cash_commitments(origin_type,origin_id)
            WHERE origin_type IN ('friday_allowance_batch','recurring_schedule_occurrence')
              AND origin_id IS NOT NULL
        """)

    payment_columns = table_columns(conn, "cash_commitment_payments")
    if "reversal_journal_entry_id" not in payment_columns:
        execute_compat(
            conn,
            "ALTER TABLE cash_commitment_payments ADD COLUMN reversal_journal_entry_id "
            "INTEGER REFERENCES journal_entries(id) ON DELETE RESTRICT"
        )
    execute_compat(
        conn,
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_cash_commitment_payment_reversal "
        "ON cash_commitment_payments(reversal_journal_entry_id)"
    )
    conn.commit()


def list_cash_commitment_duplicate_origins(conn):
    groups=execute_compat(conn,"""
        SELECT origin_type,origin_id,COUNT(*) AS duplicate_count
        FROM cash_commitments
        WHERE origin_type IN ('friday_allowance_batch','recurring_schedule_occurrence')
          AND origin_id IS NOT NULL
        GROUP BY origin_type,origin_id HAVING COUNT(*)>1 ORDER BY origin_type,origin_id
    """).fetchall()
    result=[]
    for group in groups:
        rows=execute_compat(conn,"""
            SELECT c.id,c.commitment_type,c.title,c.amount,c.due_date,c.status,
              (SELECT COUNT(*) FROM cash_commitment_payments p WHERE p.commitment_id=c.id) payment_count
            FROM cash_commitments c WHERE c.origin_type=? AND c.origin_id=? ORDER BY c.id
        """,(group["origin_type"],group["origin_id"])).fetchall()
        result.append({"origin_type":group["origin_type"],"origin_id":group["origin_id"],
                       "items":[dict(row) for row in rows]})
    return result


def resolve_cash_commitment_duplicate_origin(conn, kept_id, voided_ids, reason, reviewed_by=None):
    reason=str(reason or "").strip()
    if len(reason)<5:
        raise ValueError("اكتب سبب التسوية بوضوح (5 أحرف على الأقل).")
    kept_id=int(kept_id); voided_ids=sorted({int(v) for v in voided_ids})
    if not voided_ids or kept_id in voided_ids:
        raise ValueError("حدد الالتزام المحتفظ به والنسخ المكررة.")
    begin_write_transaction(conn)
    try:
        lock=" FOR UPDATE" if database_dialect(conn)=="postgresql" else ""
        ids=[kept_id,*voided_ids]; marks=",".join("?" for _ in ids)
        rows=execute_compat(conn,f"SELECT * FROM cash_commitments WHERE id IN ({marks}) ORDER BY id{lock}",tuple(ids)).fetchall()
        by_id={int(r["id"]):r for r in rows}
        if len(by_id)!=len(ids): raise ValueError("أحد الالتزامات المحددة غير موجود.")
        kept=by_id[kept_id]
        if kept["origin_type"] not in {"friday_allowance_batch","recurring_schedule_occurrence"} or kept["origin_id"] is None:
            raise ValueError("الالتزام المختار ليس مرتبطًا بمصدر تشغيلي.")
        if any(r["origin_type"]!=kept["origin_type"] or r["origin_id"]!=kept["origin_id"] for r in rows):
            raise ValueError("يجب أن تكون كل الالتزامات من نفس المصدر.")
        group=execute_compat(conn,"SELECT id FROM cash_commitments WHERE origin_type=? AND origin_id=?",
                             (kept["origin_type"],kept["origin_id"])).fetchall()
        if {int(r["id"]) for r in group}!=set(ids):
            raise ValueError("حدد كل سجلات المجموعة قبل اعتماد التسوية.")
        for duplicate_id in voided_ids:
            payments=execute_compat(conn,"SELECT COUNT(*) FROM cash_commitment_payments WHERE commitment_id=?",
                                    (duplicate_id,)).fetchone()[0]
            if payments:
                raise ValueError(f"الالتزام #{duplicate_id} له دفعات مرتبطة؛ اعكس الدفعات أولًا.")
            duplicate=by_id[duplicate_id]
            if duplicate["status"]!="void":
                execute_compat(conn,"""
                    UPDATE cash_commitments SET status='void',
                    notes=COALESCE(notes,'') || ?,updated_at=CURRENT_TIMESTAMP WHERE id=?
                """,(f"\nتسوية تكرار مع الالتزام #{kept_id}: {reason}",duplicate_id))
            execute_compat(conn,"""
                INSERT INTO cash_commitment_duplicate_reviews
                (origin_type,origin_id,kept_commitment_id,voided_commitment_id,reason,reviewed_by)
                VALUES(?,?,?,?,?,?)
            """,(kept["origin_type"],kept["origin_id"],kept_id,duplicate_id,reason,reviewed_by))
        remaining=execute_compat(conn,"""
            SELECT COUNT(*) FROM cash_commitments
            WHERE origin_type=? AND origin_id=? AND status<>'void'
        """,(kept["origin_type"],kept["origin_id"])).fetchone()[0]
        if remaining<=1:
            execute_compat(conn,"""
                CREATE UNIQUE INDEX IF NOT EXISTS ux_cash_commitments_source_origin
                ON cash_commitments(origin_type,origin_id)
                WHERE origin_type IN ('friday_allowance_batch','recurring_schedule_occurrence')
                  AND origin_id IS NOT NULL
            """)
        conn.commit()
        return len(voided_ids)
    except Exception:
        conn.rollback()
        raise


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
    if origin_type and origin_id is not None:
        duplicate=execute_compat(conn,"""
            SELECT id FROM cash_commitments WHERE origin_type=? AND origin_id=? AND status<>'void'
            ORDER BY id LIMIT 1
        """,(origin_type,origin_id)).fetchone()
        if duplicate:
            raise ValueError(f"يوجد التزام مرتبط بالمصدر نفسه (#{duplicate['id']}). راجع التكرارات أولًا.")
    commitment_id = insert_one_returning_id(conn,
        """INSERT INTO cash_commitments
           (commitment_type,title,period_label,amount,due_date,currency_code,
            payable_account_id,origin_type,origin_id,notes,created_by)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (commitment_type,title,str(period_label or "").strip(),str(amount),due_date,
         str(currency_code or "").strip(),payable_account_id,origin_type,origin_id,
         str(notes or "").strip(),created_by),
    )
    conn.commit()
    return commitment_id


def _journal_entry_effect_is_active(conn, journal_entry_id, as_of=None):
    """Whether a posted journal still has its original accounting effect at a date."""
    columns = table_columns(conn, "journal_entries")
    selected = ["status"]
    if "reference" in columns:
        selected.append("reference")
    if "description" in columns:
        selected.append("description")
    if "entry_date" in columns:
        selected.append("entry_date")
    entry = execute_compat(conn, 
        "SELECT " + ",".join(selected) + " FROM journal_entries WHERE id=?",
        (journal_entry_id,),
    ).fetchone()
    if not entry or entry[0] != "posted":
        return False
    # Mapping-style PostgreSQL rows iterate over column names, not values.
    # Read by key explicitly so date/reversal checks see the actual journal fields.
    values = {column: entry[column] for column in selected}
    if as_of and values.get("entry_date"):
        # psycopg may return DATE columns as datetime.date while callers pass
        # ISO strings. Compare normalized ISO dates to keep both adapters aligned.
        if str(values["entry_date"])[:10] > str(as_of)[:10]:
            return False
    reference = str(values.get("reference") or "")
    description = str(values.get("description") or "")
    if reference.startswith("REV-") or description.startswith("قيد عكسي للقيد رقم "):
        return False
    reversal_conditions, reversal_params = [], []
    if "reference" in columns:
        reversal_conditions.append("reference=?")
        reversal_params.append(f"REV-{journal_entry_id}")
    if "description" in columns:
        reversal_conditions.append("description LIKE ?")
        reversal_params.append(f"قيد عكسي للقيد رقم {journal_entry_id}:%")
    if not reversal_conditions:
        return True
    date_filter = " AND entry_date<=?" if as_of and "entry_date" in columns else ""
    if date_filter:
        reversal_params.append(as_of)
    reversal = execute_compat(conn, 
        "SELECT id FROM journal_entries WHERE status='posted' AND ("
        + " OR ".join(reversal_conditions) + ")" + date_filter + " ORDER BY id LIMIT 1",
        tuple(reversal_params),
    ).fetchone()
    if not reversal:
        return True
    restored_conditions, restored_params = [], []
    if "reference" in columns:
        restored_conditions.append("reference=?")
        restored_params.append(f"REV-{int(reversal[0])}")
    if "description" in columns:
        restored_conditions.append("description LIKE ?")
        restored_params.append(f"قيد عكسي للقيد رقم {int(reversal[0])}:%")
    if date_filter:
        restored_params.append(as_of)
    restored = execute_compat(conn, 
        "SELECT 1 FROM journal_entries WHERE status='posted' AND ("
        + " OR ".join(restored_conditions) + ")" + date_filter + " LIMIT 1",
        tuple(restored_params),
    ).fetchone()
    return restored is not None


def _payment_effect_is_active(conn, payment, as_of=None):
    reversal_id = payment["reversal_journal_entry_id"]
    if reversal_id is not None:
        reversal = execute_compat(conn, 
            "SELECT status,entry_date FROM journal_entries WHERE id=?",
            (reversal_id,),
        ).fetchone()
        reversal_date = str(reversal["entry_date"])[:10] if reversal and reversal["entry_date"] else ""
        if (reversal and reversal["status"] == "posted"
                and (not as_of or not reversal_date or reversal_date <= str(as_of)[:10])):
            return False
    return _journal_entry_effect_is_active(conn, payment["journal_entry_id"], as_of)


def cash_commitment_payment_summary(conn, commitment_id, as_of=None):
    """Summarize effective payments, optionally as they stood at a date."""
    commitment = execute_compat(conn,
        "SELECT amount,status FROM cash_commitments WHERE id=?", (commitment_id,)
    ).fetchone()
    if not commitment:
        raise ValueError("الالتزام غير موجود.")
    journal_columns = table_columns(conn, "journal_entries")
    journal_fields = ["j.status journal_status"]
    journal_fields.append("j.reference journal_reference" if "reference" in journal_columns
                          else "'' journal_reference")
    journal_fields.append("j.description journal_description" if "description" in journal_columns
                          else "'' journal_description")
    payments = execute_compat(conn,
        "SELECT p.*," + ",".join(journal_fields) +
        """ FROM cash_commitment_payments p
            LEFT JOIN journal_entries j ON j.id=p.journal_entry_id
            WHERE p.commitment_id=?
            ORDER BY p.payment_date,p.id""",
        (commitment_id,),
    ).fetchall()
    effective = Decimal("0")
    details = []
    for payment in payments:
        # payment_date is TEXT in the commitment schema, but normalize anyway
        # so PostgreSQL DATE/datetime adapters and SQLite strings behave alike.
        if as_of and str(payment["payment_date"])[:10] > str(as_of)[:10]:
            continue
        active = _payment_effect_is_active(conn, payment, as_of)
        if active:
            effective += Decimal(str(payment["amount"]))
        details.append({**dict(payment), "effective": active})
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
    begin_write_transaction(conn)
    try:
        commitment_sql = "SELECT id,amount,status,payable_account_id FROM cash_commitments WHERE id=?"
        if database_dialect(conn) == "postgresql":
            commitment_sql += " FOR UPDATE"
        commitment = execute_compat(conn, commitment_sql, (commitment_id,)).fetchone()
        if not commitment or commitment["status"] == "void":
            raise ValueError("الالتزام غير موجود أو ملغي.")
        journal = execute_compat(conn, 
            "SELECT id FROM journal_entries WHERE id=? AND status='posted'",
            (journal_entry_id,),
        ).fetchone()
        if not journal:
            raise ValueError("يجب ربط الدفعة بقيد يومية مرحّل موجود.")
        if not _journal_entry_effect_is_active(conn, journal_entry_id):
            raise ValueError("لا يمكن ربط قيد عكسي أو قيد سداد تم عكسه بالتزام.")
        selected_payable_account = _validate_payable_account(
            conn,commitment["payable_account_id"] or payable_account_id,required=True
        )
        if not selected_payable_account:
            raise ValueError("حدد حساب الالتزام المستحق قبل ربط دفعة السداد.")
        if (commitment["payable_account_id"] is not None
                and payable_account_id is not None
                and int(commitment["payable_account_id"]) != int(payable_account_id)):
            raise ValueError("حساب الالتزام لا يطابق الحساب المحدد.")
        journal_lines = execute_compat(conn, 
            "SELECT account_id,debit,credit FROM journal_lines WHERE entry_id=?",
            (journal_entry_id,),
        ).fetchall()
        payable_debit = sum((
            Decimal(str(row["debit"] or "0"))
            for row in journal_lines
            if int(row["account_id"]) == selected_payable_account
        ), Decimal("0"))
        total_debit = sum((Decimal(str(row["debit"] or "0")) for row in journal_lines), Decimal("0"))
        total_credit = sum((Decimal(str(row["credit"] or "0")) for row in journal_lines), Decimal("0"))
        if payable_debit != amount:
            raise ValueError("قيمة مدين حساب الالتزام يجب أن تساوي مبلغ الدفعة تمامًا، مع توفر مدين كافٍ.")
        if total_debit != amount or total_credit != amount:
            raise ValueError("يجب أن يكون قيد السداد مخصصًا لهذه الدفعة وحدها وبإجمالي مدين ودائن يساوي مبلغها.")
        if commitment["payable_account_id"] is None:
            execute_compat(conn, 
                "UPDATE cash_commitments SET payable_account_id=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (selected_payable_account,commitment_id),
            )
        paid = cash_commitment_payment_summary(conn, commitment_id)["paid_total"]
        total = Decimal(str(commitment["amount"]))
        if paid + amount > total:
            raise ValueError("قيمة الدفعة تتجاوز الرصيد المتبقي من الالتزام.")
        payment_id = insert_one_returning_id(conn,
            """INSERT INTO cash_commitment_payments
               (commitment_id,payment_date,amount,journal_entry_id,reference,notes,created_by)
               VALUES(?,?,?,?,?,?,?)""",
            (commitment_id,payment_date,str(amount),journal_entry_id,
             str(reference or "").strip(),str(notes or "").strip(),created_by),
        )
        status = "paid" if paid + amount == total else "partially_paid"
        execute_compat(conn, 
            "UPDATE cash_commitments SET status=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (status,commitment_id),
        )
        conn.commit()
        return payment_id
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
    begin_write_transaction(conn)
    try:
        payment_sql = """SELECT p.*,c.id commitment_id,c.amount commitment_amount,c.status commitment_status,
                      c.title commitment_title,c.payable_account_id,j.entry_date payment_journal_date
               FROM cash_commitment_payments p
               JOIN cash_commitments c ON c.id=p.commitment_id
               JOIN journal_entries j ON j.id=p.journal_entry_id
               WHERE p.id=?"""
        if database_dialect(conn) == "postgresql":
            payment_sql += " FOR UPDATE OF p,c"
        payment = execute_compat(conn, payment_sql, (payment_id,)).fetchone()
        if not payment:
            raise ValueError("دفعة الالتزام غير موجودة.")
        if payment["commitment_status"] == "void":
            raise ValueError("لا يمكن عكس دفعة مرتبطة بالتزام ملغي.")
        if payment["reversal_journal_entry_id"] is not None:
            raise ValueError("تم عكس هذه الدفعة بالفعل.")
        if reversal_date < max(payment["payment_date"], payment["payment_journal_date"] or payment["payment_date"]):
            raise ValueError("تاريخ عكس السداد لا يجوز أن يسبق تاريخ السداد أو قيده المحاسبي.")
        if not _journal_entry_effect_is_active(conn, payment["journal_entry_id"]):
            raise ValueError("قيد السداد غير مرحّل أو سبق عكسه.")
        lines = execute_compat(conn, 
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
        total_debit = sum((Decimal(str(line["debit"] or "0")) for line in lines), Decimal("0"))
        total_credit = sum((Decimal(str(line["credit"] or "0")) for line in lines), Decimal("0"))
        payment_amount = Decimal(str(payment["amount"]))
        if payable_debit != payment_amount or total_debit != payment_amount or total_credit != payment_amount:
            raise ValueError(
                "لا يمكن عكس السداد آليًا: القيد لا يطابق مبلغ الدفعة وحساب الالتزام وحدهما. "
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
        execute_compat(conn, 
            """UPDATE cash_commitment_payments
               SET reversal_journal_entry_id=? WHERE id=? AND reversal_journal_entry_id IS NULL""",
            (reversal_id, payment_id),
        )
        summary = cash_commitment_payment_summary(conn, payment["commitment_id"])
        execute_compat(conn, 
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
    begin_write_transaction(conn)
    try:
        commitment_sql = "SELECT status FROM cash_commitments WHERE id=?"
        if database_dialect(conn) == "postgresql":
            commitment_sql += " FOR UPDATE"
        row = execute_compat(conn, commitment_sql, (commitment_id,)).fetchone()
        if not row:
            raise ValueError("الالتزام غير موجود.")
        summary = cash_commitment_payment_summary(conn, commitment_id)
        if summary["paid_total"] > 0 or row["status"] == "void":
            raise ValueError("لا يمكن إلغاء التزام عليه دفعات فعالة أو تم إلغاؤه بالفعل.")
        execute_compat(conn, 
            "UPDATE cash_commitments SET status='void',updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (commitment_id,),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def list_cash_commitments_forecast(conn, as_of, end_date):
    """Return unpaid commitment balances due within the forecast horizon."""
    if not db_table_exists(conn, "cash_commitments"):
        return []
    rows = execute_compat(conn, 
        """SELECT c.* FROM cash_commitments c
           WHERE c.status!='void'
             AND c.due_date<=?
           ORDER BY c.due_date,c.id""",
        (end_date,),
    ).fetchall()
    items = []
    for row in rows:
        # Reuse the same reversal-aware payment calculation as the commitment
        # register so forecast balances cannot diverge from the displayed paid total.
        paid = cash_commitment_payment_summary(conn, row["id"], as_of)["paid_total"]
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
        return db_table_exists(conn, name)

    required_tables = {"friday_allowance_batches", "journal_entries", "cash_commitments"}
    missing = sorted(name for name in required_tables if not table_exists(name))
    if missing:
        raise ValueError("تعذر مراجعة الكشوف؛ جداول مطلوبة غير موجودة: " + "، ".join(missing))

    settings_columns = table_columns(conn, "company_settings") if table_exists("company_settings") else set()
    currency_code = ""
    if {"setting_key", "setting_value"}.issubset(settings_columns):
        setting = execute_compat(conn, 
            "SELECT setting_value FROM company_settings WHERE setting_key='base_currency' LIMIT 1"
        ).fetchone()
        currency_code = str(setting["setting_value"] or "") if setting else ""

    owns_transaction = not transaction_is_active(conn)
    savepoint = "sync_friday_allowance_commitments"
    if owns_transaction:
        begin_write_transaction(conn)
    else:
        execute_compat(conn, f"SAVEPOINT {savepoint}")
    imported = 0
    skipped = []
    try:
        batch_sql = """SELECT id,period,status,total_amount,payable_account_id,journal_entry_id
               FROM friday_allowance_batches
               WHERE status='posted' AND journal_entry_id IS NOT NULL
               ORDER BY period,id"""
        if database_dialect(conn) == "postgresql":
            batch_sql += " FOR UPDATE"
        batches = execute_compat(conn, batch_sql).fetchall()
        for batch in batches:
            batch_id = int(batch["id"])
            if execute_compat(conn, 
                """SELECT 1 FROM cash_commitments
                   WHERE origin_type='friday_allowance_batch' AND origin_id=? LIMIT 1""",
                (batch_id,),
            ).fetchone():
                skipped.append({"batch_id": batch_id, "reason": "مرتبط بالفعل"})
                continue

            journal_id = int(batch["journal_entry_id"])
            original = execute_compat(conn, 
                "SELECT id,status FROM journal_entries WHERE id=?", (journal_id,)
            ).fetchone()
            if not original or original["status"] != "posted":
                skipped.append({"batch_id": batch_id, "reason": "قيد الاستحقاق غير موجود أو غير مرحّل"})
                continue

            reversal = execute_compat(conn, 
                """SELECT id FROM journal_entries
                   WHERE reference=? AND status='posted' ORDER BY id LIMIT 1""",
                (f"REV-{journal_id}",),
            ).fetchone()
            if reversal:
                restored = execute_compat(conn, 
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

            execute_compat(conn, 
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
            execute_compat(conn, f"RELEASE SAVEPOINT {savepoint}")
        return {"imported": imported, "skipped": skipped}
    except Exception:
        if owns_transaction:
            conn.rollback()
        else:
            execute_compat(conn, f"ROLLBACK TO SAVEPOINT {savepoint}")
            execute_compat(conn, f"RELEASE SAVEPOINT {savepoint}")
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
    schedule_id = insert_one_returning_id(conn,
        """INSERT INTO cash_commitment_schedules
           (title,amount,currency_code,frequency,interval_months,anchor_day,
            next_due_date,end_date,payable_account_id,active,notes,created_by)
           VALUES(?,?,?,?,?,?,?,?,?,1,?,?)""",
        (title,str(amount),str(currency_code or "").strip(),str(frequency),
         interval_months,date.fromisoformat(first_due_date).day,first_due_date,
         normalized_end,payable_account_id,str(notes or "").strip(),created_by),
    )
    conn.commit()
    return schedule_id


def generate_recurring_schedule_occurrences(conn, schedule_id, through_date):
    """Explicitly materialize scheduled expense occurrences; safe to repeat."""
    through_date = date.fromisoformat(str(through_date)).isoformat()
    begin_write_transaction(conn)
    try:
        schedule_sql = "SELECT * FROM cash_commitment_schedules WHERE id=?"
        if database_dialect(conn) == "postgresql":
            schedule_sql += " FOR UPDATE"
        schedule = execute_compat(conn, schedule_sql, (schedule_id,)).fetchone()
        if not schedule:
            raise ValueError("جدول المصروف الدوري غير موجود.")
        next_due = schedule["next_due_date"]
        if schedule["end_date"] and next_due > schedule["end_date"]:
            if schedule["active"]:
                execute_compat(conn,
                    "UPDATE cash_commitment_schedules SET active=0,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (schedule_id,),
                )
                conn.commit()
            return 0
        if not schedule["active"]:
            raise ValueError("جدول المصروف الدوري متوقف.")
        generated = 0
        while next_due <= through_date and (
            not schedule["end_date"] or next_due <= schedule["end_date"]
        ):
            existing = execute_compat(conn, 
                """SELECT id FROM cash_commitment_schedule_occurrences
                   WHERE schedule_id=? AND due_date=?""",
                (schedule_id,next_due),
            ).fetchone()
            if not existing:
                occurrence_id = insert_one_returning_id(conn,
                    """INSERT INTO cash_commitment_schedule_occurrences
                       (schedule_id,due_date) VALUES(?,?)""",
                    (schedule_id,next_due),
                )
                commitment_id = insert_one_returning_id(conn,
                    """INSERT INTO cash_commitments
                       (commitment_type,title,period_label,amount,due_date,currency_code,
                        payable_account_id,origin_type,origin_id,notes,created_by)
                       VALUES('recurring_expense',?,?,?,?,?,?, 'recurring_schedule_occurrence',?,?,?)""",
                    (schedule["title"],next_due[:7],schedule["amount"],next_due,
                     schedule["currency_code"],schedule["payable_account_id"],
                     occurrence_id,schedule["notes"],schedule["created_by"]),
                )
                execute_compat(conn, 
                    "UPDATE cash_commitment_schedule_occurrences SET commitment_id=? WHERE id=?",
                    (commitment_id,occurrence_id),
                )
                generated += 1
            next_due = _advance_schedule_date(
                next_due,schedule["interval_months"],schedule["anchor_day"]
            )
        execute_compat(conn, 
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
    cursor = execute_compat(conn, 
        """UPDATE cash_commitment_schedules SET active=0,updated_at=CURRENT_TIMESTAMP
           WHERE id=? AND active=1""",
        (schedule_id,),
    )
    conn.commit()
    if not cursor.rowcount:
        raise ValueError("جدول المصروف الدوري غير موجود أو متوقف بالفعل.")
