from app_paths import DB_PATH
from database import connect_database
"""Customer and supplier subledger statements with opening and running balances."""
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from functools import wraps

from flask import Blueprint, abort, flash, redirect, render_template, request, session, url_for

party_bp = Blueprint("party_reports", __name__)

def _db():
    return connect_database(DB_PATH)


def _authorized(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        if ("party.view" not in set(session["permissions"])) if "permissions" in session else (session.get("role") not in {"admin", "accountant", "viewer"}):
            flash("لا تملك صلاحية عرض كشوف الحساب.", "error")
            return redirect(url_for("dashboard"))
        return fn(*args, **kwargs)
    return wrapped


def _table_exists(c, name):
    return c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _columns(c, name):
    return {row["name"] for row in c.execute(f'PRAGMA table_info("{name}")')} if _table_exists(c, name) else set()


def _events(c, kind, party_id):
    events = []
    if kind == "customer":
        for r in c.execute("""SELECT invoice_date event_date,invoice_no reference,
            'فاتورة مبيعات' description,total_amount debit,0 credit,0 priority
            FROM sales_invoices WHERE customer_id=? AND status='posted'""", (party_id,)):
            events.append(dict(r))
        for r in c.execute("""SELECT cr.receipt_date event_date,cr.receipt_no reference,
            'مقبوضات عميل ' || COALESCE(cr.reference,'') description,0 debit,
            cr.amount credit,1 priority
            FROM customer_receipts cr JOIN sales_invoices i ON i.id=cr.invoice_id
            WHERE i.customer_id=?""", (party_id,)):
            events.append(dict(r))
        if _table_exists(c, "customer_receipt_batches"):
            for r in c.execute("""SELECT receipt_date event_date,receipt_no reference,
                'تحصيل مجمع من العميل' description,0 debit,total_amount credit,1 priority
                FROM customer_receipt_batches WHERE customer_id=?""", (party_id,)):
                events.append(dict(r))
        if _table_exists(c, "customer_advance_allocations"):
            for r in c.execute("""SELECT a.allocation_date event_date,
                'CADVA-' || a.id reference,'تخصيص مقدم عميل على فاتورة ' || i.invoice_no description,
                0 debit,a.amount credit,2 priority
                FROM customer_advance_allocations a
                JOIN sales_invoices i ON i.id=a.invoice_id
                WHERE i.customer_id=?""", (party_id,)):
                events.append(dict(r))
        for r in c.execute("""SELECT cn.credit_date event_date,cn.credit_note_no reference,
            'إشعار دائن / مرتجع بيع: ' || cn.reason description,0 debit,
            cn.total_amount credit,2 priority
            FROM sales_credit_notes cn JOIN sales_invoices i ON i.id=cn.invoice_id
            WHERE i.customer_id=? AND cn.status='posted'""", (party_id,)):
            events.append(dict(r))
        if _table_exists(c, "customer_refunds"):
            for r in c.execute("""SELECT rf.refund_date event_date,rf.refund_no reference,
                'رد مبلغ للعميل عن إشعار دائن' description,rf.amount debit,0 credit,3 priority
                FROM customer_refunds rf
                JOIN sales_credit_notes cn ON cn.id=rf.credit_note_id
                JOIN sales_invoices i ON i.id=cn.invoice_id
                WHERE i.customer_id=?""", (party_id,)):
                events.append(dict(r))
        if "cancel_journal_entry_id" in _columns(c, "sales_credit_notes"):
            for r in c.execute("""SELECT je.entry_date event_date,cn.credit_note_no || '-CANCEL' reference,
                'إلغاء إشعار دائن: ' || cn.cancellation_reason description,
                cn.total_amount debit,0 credit,4 priority
                FROM sales_credit_notes cn
                JOIN sales_invoices i ON i.id=cn.invoice_id
                JOIN journal_entries je ON je.id=cn.cancel_journal_entry_id
                WHERE i.customer_id=? AND cn.status='cancelled'""", (party_id,)):
                events.append(dict(r))
    else:
        for r in c.execute("""SELECT bill_date event_date,bill_no reference,
            'فاتورة مشتريات' description,0 debit,total_amount credit,0 priority
            FROM supplier_bills WHERE supplier_id=? AND status='posted'""", (party_id,)):
            events.append(dict(r))
        for r in c.execute("""SELECT cn.credit_date event_date,cn.credit_no reference,
            'إشعار دائن مشتريات / مرتجع مورد: ' || cn.reason description,
            cn.total_amount debit,0 credit,1 priority
            FROM supplier_credit_notes cn WHERE cn.supplier_id=? AND cn.status='posted'""", (party_id,)):
            events.append(dict(r))
        for r in c.execute("""SELECT payment_date event_date,payment_no reference,
            'مدفوعات مورد ' || COALESCE(reference,'') description,amount debit,0 credit,2 priority
            FROM supplier_payments WHERE supplier_id=?""", (party_id,)):
            events.append(dict(r))
    # Templates and running-balance calculations receive Decimal values, never
    # SQLite TEXT amounts; this avoids Jinja's "%f" formatting TypeError.
    for event in events:
        event["debit"] = Decimal(str(event["debit"] or "0"))
        event["credit"] = Decimal(str(event["credit"] or "0"))
    return sorted(events, key=lambda x: (x["event_date"], x["priority"], x["reference"] or ""))


def _decimal(value):
    return Decimal(str(value or "0"))


def _gl_control_balance(c, account_codes, as_of, normal_side):
    codes = sorted({str(code) for code in account_codes if code not in (None, "")})
    if not codes:
        return None, []
    placeholders = ",".join("?" for _ in codes)
    accounts = c.execute(
        f"SELECT id,code FROM accounts WHERE code IN ({placeholders}) AND is_group=0 AND active=1",
        codes,
    ).fetchall()
    account_ids = [row["id"] for row in accounts]
    if not account_ids:
        return None, codes
    marks = ",".join("?" for _ in account_ids)
    rows = c.execute(
        f"""SELECT jl.debit,jl.credit FROM journal_lines jl
            JOIN journal_entries je ON je.id=jl.entry_id
            WHERE je.status='posted' AND je.entry_date<=?
              AND jl.account_id IN ({marks})""",
        (as_of, *account_ids),
    ).fetchall()
    debit = sum((_decimal(row["debit"]) for row in rows), Decimal("0"))
    credit = sum((_decimal(row["credit"]) for row in rows), Decimal("0"))
    return (debit - credit if normal_side == "debit" else credit - debit), [
        row["code"] for row in accounts
    ]


def _subledger_total(c, kind, as_of):
    table = "customers" if kind == "customer" else "suppliers"
    if not _table_exists(c, table):
        return Decimal("0"), 0
    party_ids = [row["id"] for row in c.execute(f"SELECT id FROM {table} ORDER BY id")]
    total = Decimal("0")
    open_parties = 0
    for party_id in party_ids:
        balance = Decimal("0")
        for event in _events(c, kind, party_id):
            if event["event_date"] and event["event_date"] <= as_of:
                amount = _decimal(event["debit"]) - _decimal(event["credit"])
                balance += amount if kind == "customer" else -amount
        total += balance
        if balance > Decimal("0.01"):
            open_parties += 1
    return total, open_parties


def _customer_advance_subledger(c, as_of):
    required = ("customer_advance_receipts", "customer_advance_allocations",
                "customer_advance_refunds")
    if not all(_table_exists(c, table) for table in required):
        return Decimal("0"), False
    receipt_columns = _columns(c, "customer_advance_receipts")
    allocation_columns = _columns(c, "customer_advance_allocations")
    refund_columns = _columns(c, "customer_advance_refunds")
    if not {"id", "receipt_date", "amount"}.issubset(receipt_columns):
        return Decimal("0"), False
    if not {"advance_receipt_id", "allocation_date", "amount"}.issubset(allocation_columns):
        return Decimal("0"), False
    if not {"advance_receipt_id", "refund_date", "amount"}.issubset(refund_columns):
        return Decimal("0"), False
    receipts = sum((_decimal(row["amount"]) for row in c.execute(
        "SELECT amount FROM customer_advance_receipts WHERE receipt_date<=?", (as_of,)
    )), Decimal("0"))
    allocations = sum((_decimal(row["amount"]) for row in c.execute(
        "SELECT amount FROM customer_advance_allocations WHERE allocation_date<=?", (as_of,)
    )), Decimal("0"))
    refunds = sum((_decimal(row["amount"]) for row in c.execute(
        "SELECT amount FROM customer_advance_refunds WHERE refund_date<=?", (as_of,)
    )), Decimal("0"))
    return receipts - allocations - refunds, True


def build_subledger_reconciliation(c, as_of):
    """Compare customer, supplier and customer-advance subledgers with GL controls."""
    from activity_profiles import get_active_activity_profile

    profile = get_active_activity_profile(c)
    posting = profile.get("posting_accounts") or {}
    roles = profile.get("account_roles") or {}
    definitions = (
        ("customers", "ذمم العملاء", (posting.get("ar"),), "debit"),
        ("suppliers", "ذمم الموردين", (posting.get("supplier_ap"),), "credit"),
    )
    results = []
    for key, label, codes, normal_side in definitions:
        subledger, party_count = _subledger_total(c, "customer" if key == "customers" else "supplier", as_of)
        gl_balance, found_codes = _gl_control_balance(c, codes, as_of, normal_side)
        difference = gl_balance - subledger if gl_balance is not None else None
        results.append({
            "key": key, "label": label, "gl_balance": gl_balance,
            "subledger_balance": subledger, "difference": difference,
            "account_codes": found_codes, "party_count": party_count,
            "available": gl_balance is not None,
            "matched": gl_balance is not None and abs(difference) <= Decimal("0.01"),
        })

    advance_codes = roles.get("customer_advances", ())
    if not isinstance(advance_codes, (list, tuple)):
        advance_codes = (advance_codes,)
    advance_subledger, advance_available = _customer_advance_subledger(c, as_of)
    advance_gl, found_advance_codes = _gl_control_balance(c, advance_codes, as_of, "credit")
    advance_difference = (
        advance_gl - advance_subledger
        if advance_gl is not None and advance_available else None
    )
    results.append({
        "key": "customer_advances", "label": "مقدمات العملاء",
        "gl_balance": advance_gl, "subledger_balance": advance_subledger,
        "difference": advance_difference, "account_codes": found_advance_codes,
        "party_count": None, "available": advance_gl is not None and advance_available,
        "matched": (advance_gl is not None and advance_available and
                    abs(advance_difference) <= Decimal("0.01")),
    })
    return results


@party_bp.route("/reports/party-statement")
@_authorized
def party_statement():
    kind = request.args.get("kind", "customer")
    if kind not in {"customer", "supplier"}:
        kind = "customer"
    party_id = request.args.get("party_id", type=int)
    date_from = request.args.get("date_from", "").strip()
    date_to = request.args.get("date_to", "").strip()
    try:
        if date_from:
            date_from = date.fromisoformat(date_from).isoformat()
        if date_to:
            date_to = date.fromisoformat(date_to).isoformat()
    except ValueError:
        abort(400)
    if date_from and date_to and date_from > date_to:
        abort(400)
    c = _db()
    parties = c.execute("SELECT id,code,name FROM customers WHERE active=1 ORDER BY code").fetchall() if kind == "customer" else c.execute(
        "SELECT id,code,name FROM suppliers WHERE active=1 ORDER BY code").fetchall()
    selected = next((p for p in parties if p["id"] == party_id), None)
    opening = Decimal("0")
    rows = []
    total_debit = Decimal("0")
    total_credit = Decimal("0")
    closing = Decimal("0")
    if selected:
        events = _events(c, kind, party_id)
        sign = Decimal("1") if kind == "customer" else Decimal("-1")
        for event in events:
            event_date = event["event_date"]
            amount = (Decimal(str(event["debit"])) - Decimal(str(event["credit"]))) * sign
            if date_from and event_date < date_from:
                opening += amount
                continue
            if date_to and event_date > date_to:
                continue
            rows.append(event)
            total_debit += Decimal(str(event["debit"]))
            total_credit += Decimal(str(event["credit"]))
        balance = opening
        for row in rows:
            balance += (Decimal(str(row["debit"])) - Decimal(str(row["credit"]))) * sign
            row["running_balance"] = balance.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        closing = balance.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    c.close()
    return render_template("party_statement.html", kind=kind, parties=parties, selected=selected,
        rows=rows, opening=opening.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        closing=closing, total_debit=total_debit.quantize(Decimal("0.01")), total_credit=total_credit.quantize(Decimal("0.01")), date_from=date_from, date_to=date_to)



def _customer_advance_details(c, as_of):
    required = ("customer_advance_receipts", "customer_advance_allocations",
                "customer_advance_refunds", "customers")
    if not all(_table_exists(c, table) for table in required):
        return [], False
    receipt_cols = _columns(c, "customer_advance_receipts")
    allocation_cols = _columns(c, "customer_advance_allocations")
    refund_cols = _columns(c, "customer_advance_refunds")
    if not {"id", "receipt_no", "customer_id", "receipt_date", "amount"}.issubset(receipt_cols):
        return [], False
    if not {"advance_receipt_id", "allocation_date", "amount"}.issubset(allocation_cols):
        return [], False
    if not {"advance_receipt_id", "refund_date", "amount"}.issubset(refund_cols):
        return [], False
    rows = c.execute(
        """SELECT r.id,r.receipt_no,r.customer_id,r.receipt_date,r.amount,
                  COALESCE(r.reference,'') reference,cu.code customer_code,cu.name customer_name
           FROM customer_advance_receipts r
           JOIN customers cu ON cu.id=r.customer_id
           WHERE r.receipt_date<=? ORDER BY r.receipt_date,r.id""",
        (as_of,),
    ).fetchall()
    result = []
    for row in rows:
        allocated = sum((_decimal(item["amount"]) for item in c.execute(
            """SELECT amount FROM customer_advance_allocations
               WHERE advance_receipt_id=? AND allocation_date<=?""",
            (row["id"], as_of),
        )), Decimal("0"))
        refunded = sum((_decimal(item["amount"]) for item in c.execute(
            """SELECT amount FROM customer_advance_refunds
               WHERE advance_receipt_id=? AND refund_date<=?""",
            (row["id"], as_of),
        )), Decimal("0"))
        received = _decimal(row["amount"])
        result.append({
            **dict(row), "received": received, "allocated": allocated,
            "refunded": refunded, "remaining": received - allocated - refunded,
        })
    return result, True


@party_bp.route("/reports/subledger-reconciliation")
@_authorized
def subledger_reconciliation():
    try:
        as_of = date.fromisoformat(
            request.args.get("as_of", "").strip() or date.today().isoformat()
        ).isoformat()
    except ValueError:
        abort(400)
    c = _db()
    try:
        rows = build_subledger_reconciliation(c, as_of)
    finally:
        c.close()
    return render_template("subledger_reconciliation.html", rows=rows, as_of=as_of)


@party_bp.route("/reports/customer-advances")
@_authorized
def customer_advance_statement():
    try:
        as_of = date.fromisoformat(
            request.args.get("as_of", "").strip() or date.today().isoformat()
        ).isoformat()
    except ValueError:
        abort(400)
    c = _db()
    try:
        rows, available = _customer_advance_details(c, as_of)
    finally:
        c.close()
    totals = {
        key: sum((row[key] for row in rows), Decimal("0"))
        for key in ("received", "allocated", "refunded", "remaining")
    }
    return render_template(
        "customer_advance_statement.html", rows=rows, totals=totals,
        as_of=as_of, available=available,
    )
