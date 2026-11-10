"""Monthly tax control report for NEXUS ERP."""
import sqlite3
from datetime import date
from decimal import Decimal
from functools import wraps
from pathlib import Path

from flask import Blueprint, flash, redirect, render_template, request, session, url_for

tax_bp = Blueprint("tax", __name__)
DB_PATH = Path(__file__).resolve().parent / "data" / "nexus.db"


def require_tax_view(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        if session.get("role") not in {"admin", "accountant", "viewer"}:
            flash("لا تملك صلاحية عرض تقرير الضرائب.", "error")
            return redirect(url_for("dashboard"))
        return fn(*args, **kwargs)
    return wrapped


@tax_bp.route("/reports/tax")
@require_tax_view
def tax_report():
    period = request.args.get("period", date.today().strftime("%Y-%m"))
    try:
        start = date.fromisoformat(period + "-01")
    except ValueError:
        period = date.today().strftime("%Y-%m")
        start = date.fromisoformat(period + "-01")
    end = date(start.year + (start.month == 12), 1 if start.month == 12 else start.month + 1, 1)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    sales = conn.execute("""
        SELECT i.invoice_no document_no, i.invoice_date document_date, c.name party_name,
               CAST(i.subtotal AS REAL) taxable_base, CAST(i.tax_rate AS REAL) tax_rate,
               CAST(i.tax_amount AS REAL) tax_amount, CAST(i.total_amount AS REAL) total_amount
        FROM sales_invoices i JOIN customers c ON c.id=i.customer_id
        WHERE i.status='posted' AND i.invoice_date>=? AND i.invoice_date<?
        ORDER BY i.invoice_date,i.id
    """, (start.isoformat(), end.isoformat())).fetchall()
    purchases = conn.execute("""
        SELECT b.bill_no document_no, b.bill_date document_date, s.name party_name,
               CAST(b.subtotal AS REAL) taxable_base, CAST(b.tax_rate AS REAL) tax_rate,
               CAST(b.tax_amount AS REAL) tax_amount, CAST(b.total_amount AS REAL) total_amount
        FROM supplier_bills b JOIN suppliers s ON s.id=b.supplier_id
        WHERE b.status='posted' AND b.bill_date>=? AND b.bill_date<?
        ORDER BY b.bill_date,b.id
    """, (start.isoformat(), end.isoformat())).fetchall()
    settings = {r["setting_key"]: r["setting_value"] for r in conn.execute(
        "SELECT setting_key,setting_value FROM company_settings WHERE setting_key IN "
        "('company_name','tax_country','tax_regime','tax_number','base_currency')")}
    conn.close()
    sales_base = sum((Decimal(str(r["taxable_base"])) for r in sales), Decimal("0"))
    output_vat = sum((Decimal(str(r["tax_amount"])) for r in sales), Decimal("0"))
    purchase_base = sum((Decimal(str(r["taxable_base"])) for r in purchases), Decimal("0"))
    input_vat = sum((Decimal(str(r["tax_amount"])) for r in purchases), Decimal("0"))
    zero_sales = [r for r in sales if Decimal(str(r["tax_rate"])) == 0]
    zero_purchases = [r for r in purchases if Decimal(str(r["tax_rate"])) == 0]
    return render_template(
        "tax_report.html", period=period, settings=settings, sales=sales, purchases=purchases,
        sales_base=sales_base, output_vat=output_vat, purchase_base=purchase_base,
        input_vat=input_vat, net_vat=output_vat-input_vat,
        zero_sales=zero_sales, zero_purchases=zero_purchases
    )
