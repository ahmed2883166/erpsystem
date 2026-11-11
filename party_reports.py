"""Customer and supplier subledger statements with opening and running balances."""
import sqlite3
from decimal import Decimal, ROUND_HALF_UP
from functools import wraps
from pathlib import Path

from flask import Blueprint, flash, redirect, render_template, request, session, url_for

party_bp = Blueprint("party_reports", __name__)
DB_PATH = Path(__file__).resolve().parent / "data" / "nexus.db"


def _db():
    c = sqlite3.connect(DB_PATH, timeout=20)
    c.row_factory = sqlite3.Row
    return c


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
    return sorted(events, key=lambda x: (x["event_date"], x["priority"], x["reference"] or ""))


@party_bp.route("/reports/party-statement")
@_authorized
def party_statement():
    kind = request.args.get("kind", "customer")
    if kind not in {"customer", "supplier"}:
        kind = "customer"
    party_id = request.args.get("party_id", type=int)
    date_from = request.args.get("date_from", "").strip()
    date_to = request.args.get("date_to", "").strip()
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
