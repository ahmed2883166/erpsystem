"""Additive register for cash commitments not already tracked by operational subledgers.

Payments link to existing posted journal entries. This module never posts GL entries.
"""
from datetime import date
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
            reference TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '',
            created_by INTEGER,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_cash_commitment_payments_commitment
            ON cash_commitment_payments(commitment_id,payment_date,id);
    """)
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


def record_cash_commitment_payment(conn, commitment_id, payment_date, amount,
                                   journal_entry_id, reference="", notes="",
                                   created_by=None):
    payment_date = date.fromisoformat(str(payment_date)).isoformat()
    amount = _amount(amount)
    conn.execute("BEGIN IMMEDIATE")
    try:
        commitment = conn.execute(
            "SELECT id,amount,status FROM cash_commitments WHERE id=?", (commitment_id,)
        ).fetchone()
        if not commitment or commitment["status"] == "void":
            raise ValueError("الالتزام غير موجود أو ملغي.")
        journal = conn.execute(
            "SELECT id FROM journal_entries WHERE id=? AND status='posted'",
            (journal_entry_id,),
        ).fetchone()
        if not journal:
            raise ValueError("يجب ربط الدفعة بقيد يومية مرحّل موجود.")
        paid = sum((Decimal(str(row["amount"])) for row in conn.execute(
            "SELECT amount FROM cash_commitment_payments WHERE commitment_id=?",
            (commitment_id,),
        ).fetchall()), Decimal("0"))
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


def void_cash_commitment(conn, commitment_id):
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT status FROM cash_commitments WHERE id=?", (commitment_id,)
        ).fetchone()
        if not row:
            raise ValueError("الالتزام غير موجود.")
        if row["status"] != "open":
            raise ValueError("لا يمكن إلغاء التزام عليه دفعات أو تم إلغاؤه بالفعل.")
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
             AND c.due_date>=? AND c.due_date<=?
           ORDER BY c.due_date,c.id""",
        (as_of,end_date),
    ).fetchall()
    items = []
    for row in rows:
        paid = sum((Decimal(str(payment["amount"])) for payment in conn.execute(
            """SELECT amount FROM cash_commitment_payments
               WHERE commitment_id=? AND payment_date<=?""",
            (row["id"],as_of),
        ).fetchall()), Decimal("0"))
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
