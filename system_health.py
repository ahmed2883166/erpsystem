"""Read-only operational and accounting health checks for NEXUS ERP."""
from decimal import Decimal, InvalidOperation
from flask import Blueprint, render_template, session, redirect, url_for, flash
from app_paths import DB_PATH
from database import connect_database

health_bp = Blueprint("system_health", __name__)


def _has_table(connection, table):
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _columns(connection, table):
    if not _has_table(connection, table):
        return set()
    return {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}


def _money(value):
    try:
        return Decimal(str(value or "0"))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def collect_health_checks(connection):
    checks = []
    def add(key, title, severity, count, detail, href):
        checks.append({"key": key, "title": title, "severity": severity,
                       "count": int(count or 0), "detail": detail, "href": href,
                       "status": "سليم" if not count else "يحتاج مراجعة"})

    # Posted journal entries must contain balanced debit and credit totals.
    unbalanced, empty = 0, 0
    if _has_table(connection, "journal_entries") and _has_table(connection, "journal_lines"):
        rows = connection.execute(
            """SELECT je.id, COUNT(jl.id) line_count,
                      COALESCE(DECIMAL_SUM(jl.debit),0) debit_total,
                      COALESCE(DECIMAL_SUM(jl.credit),0) credit_total
               FROM journal_entries je
               LEFT JOIN journal_lines jl ON jl.entry_id=je.id
               WHERE je.status='posted'
               GROUP BY je.id"""
        ).fetchall()
        for row in rows:
            if not row["line_count"]:
                empty += 1
            elif abs(_money(row["debit_total"])-_money(row["credit_total"])) > Decimal("0.01"):
                unbalanced += 1
    add("journal_balance", "القيود المرحلة غير المتوازنة", "critical", unbalanced,
        "مراجعة إجمالي المدين والدائن لكل قيد مرحّل.", url_for("reconciliation"))
    add("journal_empty", "قيود مرحلة بلا تفاصيل", "critical", empty,
        "قيد مرحّل لا يحتوي على أي سطور محاسبية.", url_for("reconciliation"))

    # Operational documents marked posted should point to a posted journal.
    for table, label, href in (
        ("sales_invoices", "فواتير المبيعات", "/sales"),
        ("supplier_bills", "فواتير الموردين", "/purchases"),
    ):
        if _has_table(connection, table):
            cols = _columns(connection, table)
            if {"status", "journal_entry_id"}.issubset(cols):
                missing = connection.execute(
                    f"""SELECT COUNT(*) FROM {table} d
                        LEFT JOIN journal_entries j ON j.id=d.journal_entry_id
                        WHERE lower(COALESCE(d.status,''))='posted'
                          AND (d.journal_entry_id IS NULL OR j.id IS NULL OR j.status<>'posted')"""
                ).fetchone()[0]
                add("missing_journal_"+table, "مستندات مرحلة دون قيد محاسبي سليم — "+label,
                    "high", missing, "مستند حالته مرحلة لكن القيد المرتبط مفقود أو غير مرحّل.",
                    href)

    # A safe stock check: identify negative remaining quantities in valuation layers.
    if _has_table(connection, "stock_valuation_layers"):
        negative = connection.execute(
            """SELECT COUNT(*) FROM stock_valuation_layers
               WHERE CAST(qty_remaining AS REAL) < -0.000001"""
        ).fetchone()[0]
        add("negative_stock_layers", "طبقات تقييم بكميات سالبة", "high", negative,
            "تحقق من حركة الصنف والطبقة قبل إجراء أي تسوية.",
            url_for("stock.valuation_reconciliation") if _has_table(connection, "stock_products") else "/")
    else:
        add("negative_stock_layers", "فحص طبقات تقييم المخزون", "info", 0,
            "جدول طبقات التقييم غير متاح في قاعدة البيانات الحالية.", "/")

    # Cross-check uses the existing detailed reconciliation page; this screen does not post fixes.
    checks.append({"key":"stock_gl_review","title":"مطابقة تقييم المخزون مع الأستاذ",
                   "severity":"info","count":None,"detail":"افتح تقرير المطابقة التفصيلي لمراجعة الفروق دون تعديل تلقائي.",
                   "href":url_for("stock.valuation_reconciliation"),"status":"متاح للمراجعة"})
    checks.append({"key":"purchase_integrity","title":"سلامة دورة المشتريات",
                   "severity":"info","count":None,"detail":"يفتح فحص الاستلامات والفواتير والدفعات في شاشة المشتريات.",
                   "href":url_for("purchase.integrity_issues"),"status":"متاح للمراجعة"})
    return checks


@health_bp.route("/system/health")
def index():
    if not session.get("user_id"):
        return redirect(url_for("login"))
    permissions = set(session.get("permissions", []))
    if "audit.view" not in permissions and session.get("role") != "admin":
        flash("لا تملك صلاحية عرض مركز سلامة النظام.", "error")
        return redirect(url_for("dashboard"))
    connection = connect_database(DB_PATH)
    try:
        checks = collect_health_checks(connection)
        audit_count = 0
        if _has_table(connection, "audit_log"):
            audit_count = connection.execute(
                "SELECT COUNT(*) FROM audit_log WHERE created_at >= datetime('now','-7 days')"
            ).fetchone()[0]
        issues = sum((item["count"] or 0) for item in checks if item["severity"] in {"critical","high"})
        summary = {"checks":len(checks), "issues":issues, "audit_week":audit_count}
    finally:
        connection.close()
    return render_template("system_health.html", checks=checks, summary=summary)
