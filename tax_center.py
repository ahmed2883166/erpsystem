from app_paths import DB_PATH
from database import connect_database
"""Tax center: auditable VAT classification and monthly control report."""
from datetime import date
from decimal import Decimal
import sqlite3
from functools import wraps

from flask import Blueprint, flash, redirect, render_template, request, session, url_for

tax_bp = Blueprint("tax", __name__)

TREATMENTS = {"standard", "zero_rated", "exempt", "out_of_scope"}


def _db():
    return connect_database(DB_PATH)


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
        if ("tax.view" not in set(session["permissions"])) if "permissions" in session else (session.get("role") not in {"admin", "accountant", "viewer"}):
            flash("لا تملك صلاحية عرض مركز الضرائب.", "error")
            return redirect(url_for("dashboard"))
        return fn(*args, **kwargs)
    return wrapped


def _editor(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        if ("tax.manage" not in set(session["permissions"])) if "permissions" in session else (session.get("role") not in {"admin", "accountant"}):
            flash("تعديل التصنيف الضريبي متاح للمحاسب ومدير النظام فقط.", "error")
            return redirect(url_for("tax.tax_report"))
        return fn(*args, **kwargs)
    return wrapped


def _party_tax_number_expr(conn, table, alias):
    """Use party tax identifiers when the installed schema has the optional column."""
    columns = {row["name"] for row in conn.execute(f'PRAGMA table_info("{table}")')}
    return f"COALESCE({alias}.tax_number,'')" if "tax_number" in columns else "''"


def _tax_ledger_reconciliation(conn, summary):
    """Compare posted VAT document totals with posted GL movement; read-only."""
    result = {
        "available": False, "output_account": "", "input_account": "",
        "output_ledger": Decimal("0"), "input_ledger": Decimal("0"),
        "output_report": summary["output_vat"],
        "input_report": Decimal("0"),
        "output_difference": Decimal("0"), "input_difference": Decimal("0"),
        "issues": [],
    }
    tables = {row["name"] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"accounts", "journal_entries", "journal_lines"}.issubset(tables):
        result["issues"].append("تعذر إجراء المطابقة: جداول الأستاذ العام غير متاحة.")
        return result
    try:
        from activity_profiles import active_tax_roles
        roles = active_tax_roles(conn)
    except (ImportError, ValueError, KeyError, TypeError):
        result["issues"].append("تعذر تحديد حسابات ضريبة المدخلات والمخرجات من ملف النشاط.")
        return result
    result["input_report"] = summary.get("purchase_tax_total", Decimal("0"))
    from accounting_core import register_decimal_aggregates
    register_decimal_aggregates(conn)
    for role, target in (("output_vat", "output"), ("input_vat", "input")):
        code = roles.get(role) if isinstance(roles, dict) else None
        if isinstance(code, (list, tuple)):
            codes = [str(x) for x in code if x]
        else:
            codes = [str(code)] if code else []
        if not codes:
            result["issues"].append("لم يتم تحديد حساب دفتر الأستاذ لدور " + role + ".")
            continue
        placeholders = ",".join("?" for _ in codes)
        rows = conn.execute(f"""
            SELECT a.code,
                   COALESCE(DECIMAL_SUM(jl.debit),0) debit,
                   COALESCE(DECIMAL_SUM(jl.credit),0) credit
            FROM accounts a
            LEFT JOIN journal_entries je ON je.status='posted'
                 AND je.entry_date>=? AND je.entry_date<?
            LEFT JOIN journal_lines jl ON jl.account_id=a.id AND jl.entry_id=je.id
            WHERE a.code IN ({placeholders})
            GROUP BY a.id,a.code
        """, (summary["period_start"], summary["period_end"], *codes)).fetchall()
        debit = sum((Decimal(str(r["debit"] or "0")) for r in rows), Decimal("0"))
        credit = sum((Decimal(str(r["credit"] or "0")) for r in rows), Decimal("0"))
        ledger_value = credit-debit if target == "output" else debit-credit
        result[target+"_account"] = ", ".join(codes)
        result[target+"_ledger"] = ledger_value
        report_value = result[target+"_report"]
        result[target+"_difference"] = ledger_value-report_value
        if abs(result[target+"_difference"]) > Decimal("0.02"):
            result["issues"].append(
                f"فرق مطابقة {('المخرجات' if target == 'output' else 'المدخلات')}: "
                f"{result[target+'_difference']:.2f}"
            )
    result["available"] = True
    return result


def _period(value):
    try:
        start = date.fromisoformat(value + "-01")
    except (TypeError, ValueError):
        start = date.today().replace(day=1)
        value = start.strftime("%Y-%m")
    end = date(start.year + (start.month == 12), 1 if start.month == 12 else start.month + 1, 1)
    return value, start, end


def _build_saudi_tax_preparation(conn, period, summary):
    """Annual accounting figures for Saudi tax workpapers; deliberately no liability calculation."""
    year = int(period[:4])
    date_from, date_to = f"{year}-01-01", f"{year}-12-31"
    result = {
        "available": False, "year": year, "date_from": date_from, "date_to": date_to,
        "revenue_total": Decimal("0"), "cogs_total": Decimal("0"),
        "gross_profit": Decimal("0"), "operating_expense_total": Decimal("0"),
        "net_profit": Decimal("0"), "output_vat": summary["output_vat"],
        "input_vat": summary["input_vat"], "net_vat": summary["net_vat"],
        "total_assets": Decimal("0"), "total_liabilities": Decimal("0"),
        "equity_before_profit": Decimal("0"), "total_equity": Decimal("0"),
        "balance_difference": Decimal("0"), "balance_sheet_balanced": False,
        "negative_asset_accounts": [],
        "zakat_or_income_tax": None,
        "notice": "بيانات محاسبية مساندة فقط؛ لم يتم احتساب وعاء الزكاة أو ضريبة الدخل أو الضريبة المستحقة.",
    }
    try:
        from income_statement import build_income_statement
        from financial_planning import build_balance_sheet_snapshot
        from accounting_core import register_decimal_aggregates
        register_decimal_aggregates(conn)
        statement = build_income_statement(conn, date_from, date_to)
        balance_sheet = build_balance_sheet_snapshot(conn, date_to)
        for key in ("revenue_total", "cogs_total", "gross_profit",
                    "operating_expense_total", "net_profit"):
            result[key] = statement[key]
        result.update({
            "total_assets": balance_sheet["total_assets"],
            "total_liabilities": balance_sheet["total_liabilities"],
            "equity_before_profit": balance_sheet["equity_before_profit"],
            "total_equity": balance_sheet["total_equity"],
            "balance_difference": balance_sheet["balance_difference"],
            "balance_sheet_balanced": balance_sheet["balanced"],
            "negative_asset_accounts": balance_sheet["negative_asset_accounts"],
        })
        result["available"] = True
    except (ImportError, sqlite3.Error, KeyError, ValueError) as exc:
        result["notice"] = "تعذر استخراج بيانات القوائم السنوية من الأستاذ العام: " + str(exc)
    return result


def build_tax_report_snapshot(conn, period_value):
    period, start, end = _period(period_value)
    customer_tax_number = _party_tax_number_expr(conn, "customers", "c")
    sales = conn.execute(f"""
        SELECT i.id, i.invoice_no document_no, i.invoice_date document_date, c.name party_name, {customer_tax_number} party_tax_number,
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
        credit_rows = conn.execute(f"""
            SELECT cn.id, cn.credit_note_no document_no, cn.credit_date document_date,
                   c.name party_name, {customer_tax_number} party_tax_number,
                   cl.line_total taxable_base, cl.tax_amount,
                   cl.tax_treatment, cn.reason tax_notes
            FROM sales_credit_notes cn
            JOIN sales_invoices i ON i.id=cn.invoice_id
            JOIN customers c ON c.id=i.customer_id
            JOIN sales_credit_note_lines cl ON cl.credit_note_id=cn.id
            WHERE cn.status='posted' AND cn.credit_date>=? AND cn.credit_date<?
            ORDER BY cn.credit_date,cn.id,cl.id
        """, (start.isoformat(), end.isoformat())).fetchall()
        grouped = {}
        for row in credit_rows:
            key = (row["id"], row["document_no"], row["document_date"],
                   row["party_name"], row["party_tax_number"], row["tax_treatment"] or "standard", row["tax_notes"])
            item = grouped.setdefault(key, {"taxable_base": Decimal("0"),
                                             "tax_amount": Decimal("0")})
            item["taxable_base"] += Decimal(row["taxable_base"] or "0")
            item["tax_amount"] += Decimal(row["tax_amount"] or "0")
        for key, amounts in grouped.items():
            credit_id, doc_no, doc_date, party_name, party_tax_number, treatment, notes = key
            base, tax_value = amounts["taxable_base"], amounts["tax_amount"]
            sales_credits.append({
                "id": credit_id, "document_no": doc_no, "document_date": doc_date,
                "party_name": party_name, "party_tax_number": party_tax_number, "taxable_base": -base,
                "tax_amount": -tax_value,
                "tax_rate": (tax_value * Decimal("100") / base).quantize(Decimal("0.01"))
                            if base else Decimal("0"),
                "total_amount": -(base + tax_value), "tax_treatment": treatment,
                "tax_reference": "", "tax_notes": notes,
                "document_type": "إشعار دائن",
            })
    supplier_tax_number = _party_tax_number_expr(conn, "suppliers", "s")
    purchases = conn.execute(f"""
        SELECT b.id, b.bill_no document_no, b.bill_date document_date, s.name party_name, {supplier_tax_number} party_tax_number,
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
        "tax_rate": Decimal(r["tax_rate"] or "0"),
        "total_amount": Decimal(r["total_amount"] or "0")
    } for r in sales]
    sales = [{**r, "document_type": "فاتورة بيع"} for r in sales] + sales_credits
    purchases = [{**dict(r), "taxable_base": Decimal(r["taxable_base"] or "0"),
                      "tax_rate": Decimal(r["tax_rate"] or "0"), "tax_amount": Decimal(r["tax_amount"] or "0"),
                      "total_amount": Decimal(r["total_amount"] or "0"),
                      "deductible_percent": Decimal(r["deductible_percent"] or "100")} for r in purchases]
    settings = {r["setting_key"]: r["setting_value"] for r in conn.execute(
        "SELECT setting_key,setting_value FROM company_settings WHERE setting_key IN "
        "('company_name','tax_country','tax_regime','tax_number','base_currency')")}

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
        "purchase_tax_total": sum((Decimal(str(r["tax_amount"])) for r in purchases), Decimal("0")),
        "output_vat": output_vat, "input_vat": input_vat,
        "non_deductible_input": non_deductible_input, "net_vat": output_vat-input_vat,
    }
    summary["period_start"] = start.isoformat()
    summary["period_end"] = end.isoformat()
    ledger_reconciliation = _tax_ledger_reconciliation(conn, summary)
    saudi_preparation = (
        _build_saudi_tax_preparation(conn, period, summary)
        if settings.get("tax_country") == "SA" else None
    )
    # Advisory data-quality checks. These never modify posted documents or journals.
    quality_issues = []
    company_tax_number = (settings.get("tax_number") or "").strip()
    country = settings.get("tax_country", "EG")
    if not company_tax_number:
        quality_issues.append({"severity": "warning", "document": "بيانات الشركة",
                               "issue": "الرقم الضريبي للشركة غير مسجل"})
    elif country == "EG" and not (company_tax_number.isdigit() and len(company_tax_number) == 9):
        quality_issues.append({"severity": "warning", "document": "بيانات الشركة",
                               "issue": "الرقم الضريبي المصري لا يطابق الشكل المتوقع (9 أرقام)"})
    elif country == "SA" and not (company_tax_number.isdigit() and len(company_tax_number) == 15):
        quality_issues.append({"severity": "warning", "document": "بيانات الشركة",
                               "issue": "رقم التسجيل الضريبي السعودي لا يطابق الشكل المتوقع (15 رقمًا)"})
    for kind, rows in (("مبيعات", sales), ("مشتريات", purchases)):
        for row in rows:
            doc = f"{kind} {row['document_no']} - {row['document_date']}"
            if not (row.get("party_tax_number") or "").strip():
                quality_issues.append({"severity": "warning", "document": doc,
                                       "issue": "الرقم الضريبي للطرف غير مسجل"})
            base = Decimal(str(row["taxable_base"] or "0"))
            rate = Decimal(str(row["tax_rate"] or "0"))
            actual_tax = Decimal(str(row["tax_amount"] or "0"))
            expected_tax = (base * rate / Decimal("100")).quantize(Decimal("0.01"))
            if abs(expected_tax - actual_tax) > Decimal("0.02"):
                quality_issues.append({"severity": "error", "document": doc,
                                       "issue": f"فرق بين الضريبة المحسوبة والمسجلة: {actual_tax - expected_tax}"})
            expected_total = Decimal(str(row["total_amount"] or "0"))
            if abs((base + actual_tax) - expected_total) > Decimal("0.02"):
                quality_issues.append({"severity": "error", "document": doc,
                                       "issue": "الإجمالي لا يساوي الوعاء الضريبي مضافًا إليه الضريبة"})
    for row in inconsistent_sales + inconsistent_purchases:
        quality_issues.append({"severity": "error", "document": str(row["document_no"]),
                               "issue": "التصنيف الضريبي لا يتفق مع قيمة/نسبة الضريبة"})
    return {
        "period": period, "settings": settings, "sales": sales,
        "sales_credits": sales_credits, "purchases": purchases,
        "summary": summary, "inconsistent_sales": inconsistent_sales,
        "inconsistent_purchases": inconsistent_purchases,
        "quality_issues": quality_issues,
        "ledger_reconciliation": ledger_reconciliation,
        "saudi_preparation": saudi_preparation,
    }


@tax_bp.route("/reports/tax")
@_authorized
def tax_report():
    period, _, _ = _period(request.args.get("period", date.today().strftime("%Y-%m")))
    conn = _db()
    try:
        snapshot = build_tax_report_snapshot(conn, period)
    finally:
        conn.close()
    return render_template("tax_report.html", **snapshot, treatments=TREATMENTS)


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
