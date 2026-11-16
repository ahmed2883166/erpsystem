"""Read-only operational and accounting health checks for NEXUS ERP."""
from decimal import Decimal, InvalidOperation
from flask import Blueprint, render_template, session, redirect, url_for, flash
from app_paths import DB_PATH
from database import connect_database
from accounting_core import reconciliation_snapshot

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
                if _has_table(connection, "journal_entries"):
                    missing = connection.execute(
                        f"""SELECT COUNT(*) FROM {table} d
                            LEFT JOIN journal_entries j ON j.id=d.journal_entry_id
                            WHERE lower(COALESCE(d.status,''))='posted'
                              AND (d.journal_entry_id IS NULL OR j.id IS NULL OR j.status<>'posted')"""
                    ).fetchone()[0]
                else:
                    # If the journal table is absent, every posted source document is unlinked.
                    missing = connection.execute(
                        f"""SELECT COUNT(*) FROM {table}
                            WHERE lower(COALESCE(status,''))='posted'"""
                    ).fetchone()[0]
                add("missing_journal_"+table, "مستندات مرحلة دون قيد محاسبي سليم — "+label,
                    "high", missing, "مستند حالته مرحلة لكن القيد المرتبط مفقود أو غير مرحّل.",
                    href)

    # A safe stock check: identify negative remaining quantities in valuation layers.
    if _has_table(connection, "stock_valuation_layers"):
        if "qty_remaining" in _columns(connection, "stock_valuation_layers"):
            negative = connection.execute(
                """SELECT COUNT(*) FROM stock_valuation_layers
                   WHERE CAST(qty_remaining AS REAL) < -0.000001"""
            ).fetchone()[0]
            add("negative_stock_layers", "طبقات تقييم بكميات سالبة", "high", negative,
                "تحقق من حركة الصنف والطبقة قبل إجراء أي تسوية.",
                url_for("stock.valuation_reconciliation") if _has_table(connection, "stock_products") else "/")
        else:
            add("negative_stock_layers", "فحص طبقات تقييم المخزون", "info", 0,
                "عمود الكمية المتبقية غير متاح في جدول طبقات التقييم.", "/")
    else:
        add("negative_stock_layers", "فحص طبقات تقييم المخزون", "info", 0,
            "جدول طبقات التقييم غير متاح في قاعدة البيانات الحالية.", "/")

    # Backup readiness: report persisted scheduler state without triggering a backup.
    if _has_table(connection, "company_settings"):
        backup_settings = {row["setting_key"]: row["setting_value"] for row in connection.execute(
            "SELECT setting_key,setting_value FROM company_settings WHERE setting_key LIKE 'backup_%'"
        )}
        enabled = backup_settings.get("backup_enabled", "0") == "1"
        last_status = (backup_settings.get("backup_last_status") or "").strip().lower()
        last_run = (backup_settings.get("backup_last_run") or "").strip()
        if not enabled:
            add("backup_readiness", "النسخ الاحتياطي التلقائي", "high", 1,
                "النسخ التلقائي غير مفعل. راجع الإعدادات وفعل الجدول بعد تحديد مجلد آمن.", "/settings/backup")
        elif last_status == "failed":
            add("backup_readiness", "النسخ الاحتياطي التلقائي", "critical", 1,
                "آخر محاولة نسخ فشلت: " + (backup_settings.get("backup_last_error") or "لا يوجد وصف")[:240], "/settings/backup")
        elif last_status == "success" and last_run:
            add("backup_readiness", "النسخ الاحتياطي التلقائي", "info", 0,
                "آخر نسخة ناجحة: " + last_run + " — تحقق دوريًا من إمكانية الاستعادة.", "/settings/backup")
        else:
            add("backup_readiness", "النسخ الاحتياطي التلقائي", "high", 1,
                "لم تُسجل نسخة ناجحة حتى الآن أو لم تكتمل أول دورة نسخ.", "/settings/backup")
    else:
        add("backup_readiness", "النسخ الاحتياطي التلقائي", "info", 0,
            "إعدادات النسخ الاحتياطي غير متاحة في قاعدة البيانات الحالية.", "/")

    # Prefer the append-only audit log: the temporary login_attempts table is
    # pruned after the 15-minute lockout window and cannot represent 24 hours.
    login_audit_columns = _columns(connection, "audit_log")
    if {"created_at", "action", "outcome", "entity_type"}.issubset(login_audit_columns):
        failed_logins = connection.execute(
            """SELECT COUNT(*) FROM audit_log
               WHERE datetime(created_at) >= datetime('now','-24 hours')
                 AND (action IN ('login_failed','login_blocked')
                      OR (entity_type='auth' AND outcome='failure'))"""
        ).fetchone()[0]
        login_source = "سجل التدقيق المحفوظ"
    elif _has_table(connection, "login_attempts") and {
        "attempted_at", "username", "remote_addr"
    }.issubset(_columns(connection, "login_attempts")):
        failed_logins = connection.execute(
            "SELECT COUNT(*) FROM login_attempts WHERE datetime(attempted_at) >= datetime('now','-24 hours')"
        ).fetchone()[0]
        login_source = "سجل المحاولات المؤقت؛ قد لا يشمل المحاولات الأقدم من 15 دقيقة"
    else:
        failed_logins = 0
        login_source = "سجل محاولات الدخول غير متاح في قاعدة البيانات الحالية"
    add("recent_login_attempts", "محاولات الدخول الفاشلة خلال آخر 24 ساعة",
        "high" if failed_logins >= 20 else "info", failed_logins,
        f"{login_source}. راجع المستخدم وعنوان الشبكة والتوقيت؛ لا يتم قفل أي حساب تلقائيًا من هذا التقرير.",
        "/security/activity-monitor?category=login")

    # Count approval work waiting for a decision when the approval queue exists.
    if _has_table(connection, "approval_requests"):
        approval_columns = _columns(connection, "approval_requests")
        if "status" in approval_columns:
            pending_approvals = connection.execute(
                "SELECT COUNT(*) FROM approval_requests WHERE lower(COALESCE(status,'')) IN ('pending','requested','submitted')"
            ).fetchone()[0]
            add("pending_approvals", "طلبات الموافقة المعلقة", "info", pending_approvals,
                "راجع مركز الموافقات لتجنب تأخر المستندات؛ لا يتم اعتماد أي طلب تلقائيًا.", "/approvals")

    # Reconcile the posted GRNI control account against still-uninvoiced posted receipts.
    # This is diagnostic only: never creates a journal or changes source documents.
    if _has_table(connection, "goods_receipts") and _has_table(connection, "supplier_bill_receipts"):
        snapshot = reconciliation_snapshot(connection)
        if snapshot.get("grni_ledger") is not None:
            grni_difference = snapshot.get("grni_difference") or Decimal("0")
            overallocated = snapshot.get("grni_overallocated_receipts") or []
            issue_count = int(grni_difference != 0 or bool(overallocated))
            detail = (
                f"رصيد حساب GRNI: {snapshot['grni_ledger']:.2f}؛ "
                f"المتبقي من إيصالات الاستلام غير المفوترة: {snapshot['grni']:.2f}؛ "
                f"فرق المطابقة: {grni_difference:.2f}. "
                f"قيمة طبقات الاستلام المتبقية: {snapshot['stock_value']:.2f}؛ "
                f"قيمة الصرف المقدرة من طبقات الإيصالات: {snapshot['grni_consumed_value']:.2f}."
            )
            if overallocated:
                detail += f" يوجد {len(overallocated)} إيصال/إيصالات بتخصيص فواتير يتجاوز قيمة الاستلام."
            add("grni_reconciliation", "مطابقة بضاعة مستلمة غير مفوترة GRNI",
                "high" if issue_count else "info", issue_count, detail, "/purchases")

    # Validate the latest financial-year opening against the prior ledger.
    # The underlying pre-close report is read-only and blocks close on mismatched balances.
    if _has_table(connection, "financial_years"):
        year_columns = _columns(connection, "financial_years")
        if {"id", "year_name", "start_date", "opening_entry_id"}.issubset(year_columns):
            year = connection.execute("""SELECT id,year_name,start_date
                FROM financial_years WHERE opening_entry_id IS NOT NULL
                ORDER BY start_date DESC,id DESC LIMIT 1""").fetchone()
            if year:
                try:
                    from financial_planning import build_financial_year_preclose_report
                    report = build_financial_year_preclose_report(connection, year["id"])
                    opening_checks = {
                        item["code"]: item for item in report.get("checks", [])
                        if item.get("code") in {
                            "opening_integrity", "opening_balance_reconciliation"
                        }
                    }
                    for code, title in (
                        ("opening_integrity", "سلامة قيد افتتاح السنة"),
                        ("opening_balance_reconciliation", "مطابقة أرصدة الافتتاح مع الأستاذ"),
                    ):
                        item = opening_checks.get(code)
                        if item:
                            add(code, title,
                                "high" if item["count"] else "info",
                                item["count"], f"السنة {year['year_name']}: {item['details']}",
                                "/financial-years")
                except (sqlite3.Error, KeyError, TypeError, ValueError):
                    add("opening_balance_reconciliation", "مطابقة أرصدة الافتتاح مع الأستاذ",
                        "high", 1,
                        f"تعذر التحقق من افتتاح السنة {year['year_name']}؛ يلزم مراجعة تقرير ما قبل الإقفال.",
                        "/financial-years")

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
        # Hide links the current user cannot open; the health checks themselves remain read-only.
        is_admin = session.get("role") == "admin"
        can_reports = is_admin or "reports.view" in permissions
        can_stock = is_admin or "stock.view" in permissions
        can_purchases = is_admin or "purchases.view" in permissions
        can_security = is_admin or "audit.view" in permissions
        can_settings = is_admin or "settings.view" in permissions
        can_approvals = is_admin or "approvals.view" in permissions
        restricted_links = {
            "journal_balance": can_reports,
            "journal_empty": can_reports,
            "negative_stock_layers": can_stock,
            "stock_gl_review": can_stock,
            "purchase_integrity": can_purchases,
            "recent_login_attempts": can_security,
            "backup_readiness": can_settings,
            "pending_approvals": can_approvals,
        }
        for item in checks:
            if item["key"] in restricted_links and not restricted_links[item["key"]]:
                item["href"] = None
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
