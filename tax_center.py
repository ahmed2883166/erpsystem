"""Tax center: auditable VAT classification and monthly control report."""
import sqlite3
from datetime import date
from decimal import Decimal
from functools import wraps
from pathlib import Path

from flask import Blueprint, flash, redirect, render_template, request, session, url_for

tax_bp = Blueprint("tax", __name__)
DB_PATH = Path(__file__).resolve().parent / "data" / "nexus.db"
TREATMENTS = {"standard", "zero_rated", "exempt", "out_of_scope"}


def _db():
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def initialize_tax_schema():
    """Additive migration; never rewrites existing invoice values."""
    conn = _db()
    for table in ("sales_invoices", "supplier_bills"):
        cols = {r["name"] for r in conn.execute('PRAGMA table_info("' + table + '")')}
        if not cols:
            continue
        additions = {
            "tax_treatment": "TEXT NOT NULL DEFAULT 'standard'",
            "tax_reference": "TEXT NOT NULL DEFAULT ''",
            "tax_notes": "TEXT NOT NULL DEFAULT ''",
        }
        if table == "supplier_bills":
            additions["input_vat_deductible_percent"] = "TEXT NOT NULL DEFAULT '100'"
        for name, definition in additions.items():
            if name not in cols:
                conn.execute('ALTER TABLE "' + table + '" ADD COLUMN "' + name + '" ' + definition)
    conn.commit()
    conn.close()


def _authorized(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        if session.get("role") not in {"admin", "accountant", "viewer"}:
            flash("لا تملك صلاحية عرض مركز الضرائب.", "error")
            return redirect(url_for("dashboard"))
        return fn(*args, **kwargs)
    return wrapped


def _editor(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        if session.get("role") not in {"admin", "accountant"}:
            flash("تعديل التصنيف الضريبي متاح للمحاسب ومدير النظام فقط.", "error")
            return redirect(url_for("tax.tax_report"))
        return fn(*args, **kwargs)
    return wrapped


def _period(value):
    try:
        start = date.fromisoformat(value + "-01")
    except (TypeError, ValueError):
        start = date.today().replace(day=1)
        value = start.strftime("%Y-%m")
    end = date(start.year + (start.month == 12), 1 if start.month == 12 else start.month + 1, 1)
    return value, start, end


@tax_bp.route("/reports/tax")
@_authorized
def tax_report():
    period, start, end = _period(request.args.get("period", date.today().strftime("%Y-%m")))
    conn = _db()
    sales = conn.execute("""
        SELECT i.id, i.invoice_no document_no, i.invoice_date document_date, c.name party_name,
               i.subtotal taxable_base, i.tax_rate tax_rate,
               i.tax_amount tax_amount, i.total_amount total_amount,
               COALESCE(i.tax_treatment,'standard') tax_treatment,
               COALESCE(i.tax_reference,'') tax_reference, COALESCE(i.tax_notes,'') tax_notes
        FROM sales_invoices i JOIN customers c ON c.id=i.customer_id
        WHERE i.status='posted' AND i.invoice_date>=? AND i.invoice_date<?
        ORDER BY i.invoice_date,i.id
    """, (start.isoformat(), end.isoformat())).fetchall()
    # Posted sales credit notes reduce output VAT in the period of the credit note.
    # Group by the original line tax treatment so mixed-treatment invoices remain classified correctly.
    sales_credits = []
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sales_credit_notes'").fetchone():
        sales_credits = conn.execute("""
            SELECT cn.id, cn.credit_note_no document_no, cn.credit_date document_date,
                   c.name party_name, SUM(CAST(cl.line_total AS NUMERIC)) taxable_base,
                   SUM(CAST(cl.tax_amount AS NUMERIC)) tax_amount,
                   SUM(CAST(cl.line_total AS NUMERIC)+CAST(cl.tax_amount AS NUMERIC)) total_amount,
                   COALESCE(cl.tax_treatment,'standard') tax_treatment,
                   '' tax_reference, cn.reason tax_notes
            FROM sales_credit_notes cn
            JOIN sales_invoices i ON i.id=cn.invoice_id
            JOIN customers c ON c.id=i.customer_id
            JOIN sales_credit_note_lines cl ON cl.credit_note_id=cn.id
            WHERE cn.status='posted' AND cn.credit_date>=? AND cn.credit_date<?
            GROUP BY cn.id, COALESCE(cl.tax_treatment,'standard')
            ORDER BY cn.credit_date,cn.id
        """, (start.isoformat(), end.isoformat())).fetchall()
    purchases = conn.execute("""
        SELECT b.id, b.bill_no document_no, b.bill_date document_date, s.name party_name,
               b.subtotal taxable_base, b.tax_rate tax_rate,
               b.tax_amount tax_amount, b.total_amount total_amount,
               COALESCE(b.tax_treatment,'standard') tax_treatment,
               COALESCE(b.tax_reference,'') tax_reference, COALESCE(b.tax_notes,'') tax_notes,
               COALESCE(b.input_vat_deductible_percent,'100') deductible_percent
        FROM supplier_bills b JOIN suppliers s ON s.id=b.supplier_id
        WHERE b.status='posted' AND b.bill_date>=? AND b.bill_date<?
        ORDER BY b.bill_date,b.id
    """, (start.isoformat(), end.isoformat())).fetchall()
    sales = [{
        **dict(r),
        "taxable_base": Decimal(r["taxable_base"] or "0"),
        "tax_amount": Decimal(r["tax_amount"] or "0"),
        "tax_rate": (
            Decimal(r["tax_amount"] or "0") * Decimal("100") / Decimal(r["taxable_base"])
        ).quantize(Decimal("0.01")) if Decimal(r["taxable_base"] or "0") else Decimal("0"),
        "total_amount": Decimal(r["total_amount"] or "0")
    } for r in sales]
    sales_credits = [{
        **dict(r),
        "taxable_base": -Decimal(r["taxable_base"] or "0"),
        "tax_amount": -Decimal(r["tax_amount"] or "0"),
        "tax_rate": (Decimal(r["tax_amount"] or "0") * Decimal("100") /
                     Decimal(r["taxable_base"])).quantize(Decimal("0.01"))
                    if Decimal(r["taxable_base"] or "0") else Decimal("0"),
        "total_amount": -Decimal(r["total_amount"] or "0"),
        "document_type": "إشعار دائن",
    } for r in sales_credits]
    sales = [{**r, "document_type": "فاتورة بيع"} for r in sales] + sales_credits
    purchases = [{**dict(r), "taxable_base": Decimal(r["taxable_base"] or "0"),
                      "tax_rate": Decimal(r["tax_rate"] or "0"), "tax_amount": Decimal(r["tax_amount"] or "0"),
                      "total_amount": Decimal(r["total_amount"] or "0"),
                      "deductible_percent": Decimal(r["deductible_percent"] or "100")} for r in purchases]
    settings = {r["setting_key"]: r["setting_value"] for r in conn.execute(
        "SELECT setting_key,setting_value FROM company_settings WHERE setting_key IN "
        "('company_name','tax_country','tax_regime','tax_number','base_currency')")}
    conn.close()

    zero_sales = [r for r in sales if r["tax_treatment"] == "zero_rated"]
    exempt_sales = [r for r in sales if r["tax_treatment"] == "exempt"]
    outside_sales = [r for r in sales if r["tax_treatment"] == "out_of_scope"]
    standard_sales = [r for r in sales if r["tax_treatment"] == "standard"]
    inconsistent_sales = [r for r in sales if r["tax_treatment"] != "standard" and
                          (Decimal(str(r["tax_rate"])) != 0 or Decimal(str(r["tax_amount"])) != 0)]
    inconsistent_purchases = [r for r in purchases if r["tax_treatment"] != "standard" and
                               Decimal(str(r["tax_amount"])) != 0]
    deductible_input = sum((Decimal(str(r["tax_amount"])) * Decimal(str(r["deductible_percent"])) / Decimal("100")
                            for r in purchases if r["tax_treatment"] == "standard"), Decimal("0"))
    non_deductible_input = sum((
        Decimal(str(r["tax_amount"])) if r["tax_treatment"] != "standard" else
        Decimal(str(r["tax_amount"])) * (Decimal("100")-Decimal(str(r["deductible_percent"]))) / Decimal("100")
        for r in purchases), Decimal("0"))
    # Keep the posted tax in the report; classification mismatches must be corrected by accounting documents.
    output_vat = sum((Decimal(str(r["tax_amount"])) for r in sales), Decimal("0"))
    input_vat = deductible_input
    summary = {
        "sales_standard": sum((Decimal(str(r["taxable_base"])) for r in standard_sales), Decimal("0")),
        "sales_zero": sum((Decimal(str(r["taxable_base"])) for r in zero_sales), Decimal("0")),
        "sales_exempt": sum((Decimal(str(r["taxable_base"])) for r in exempt_sales), Decimal("0")),
        "sales_outside": sum((Decimal(str(r["taxable_base"])) for r in outside_sales), Decimal("0")),
        "purchase_base": sum((Decimal(str(r["taxable_base"])) for r in purchases), Decimal("0")),
        "output_vat": output_vat, "input_vat": input_vat,
        "non_deductible_input": non_deductible_input, "net_vat": output_vat-input_vat,
    }
    return render_template("tax_report.html", period=period, settings=settings, sales=sales, sales_credits=sales_credits,
        purchases=purchases, summary=summary, inconsistent_sales=inconsistent_sales,
        inconsistent_purchases=inconsistent_purchases, treatments=TREATMENTS)


@tax_bp.route("/reports/tax/classify/<kind>/<int:document_id>", methods=["POST"])
@_editor
def classify_document(kind, document_id):
    if kind not in {"sale", "purchase"}:
        flash("نوع المستند غير صحيح.", "error")
        return redirect(url_for("tax.tax_report"))
    table = "sales_invoices" if kind == "sale" else "supplier_bills"
    conn = _db()
    row = conn.execute(
        "SELECT id FROM " + table + " WHERE id=? AND status='posted'",
        (document_id,)
    ).fetchone()
    conn.close()
    if not row:
        flash("المستند غير موجود أو غير مرحل.", "error")
        return redirect(url_for("tax.tax_report", period=request.form.get("period", "")))
    # Posted tax classifications are immutable until an auditable tax-adjustment
    # document can update both the GL and the tax return together.
    flash("لا يمكن تعديل التصنيف أو نسبة خصم الضريبة بعد ترحيل المستند. أنشئ مستند تسوية ضريبية معتمدًا حتى تظل الدفاتر والإقرار متطابقين.", "error")
    return redirect(url_for("tax.tax_report", period=request.form.get("period", "")))
