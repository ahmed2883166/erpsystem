"""Configurable country-specific payroll tax and social insurance rules.

Rates are deliberately data-driven: the application does not embed statutory
rates that may become obsolete. Rule sets are effective-dated and snapshotted
onto payroll lines when a run is created.
"""
import sqlite3
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import wraps
from pathlib import Path

from flask import Blueprint, flash, redirect, render_template, request, session, url_for

payroll_compliance_bp = Blueprint("payroll_compliance", __name__)
DB_PATH = Path(__file__).resolve().parent / "data" / "nexus.db"
CENT = Decimal("0.01")


def _d(value):
    try:
        result = Decimal(str(value or "0"))
        if not result.is_finite():
            raise InvalidOperation()
        return result
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("قيمة قواعد الرواتب غير صحيحة.")


def _m(value):
    return _d(value).quantize(CENT, rounding=ROUND_HALF_UP)


def _db():
    conn = sqlite3.connect(DB_PATH, timeout=20, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=20000")
    return conn


def initialize_payroll_compliance_schema():
    conn = _db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS payroll_rule_sets(
      id INTEGER PRIMARY KEY,
      country_code TEXT NOT NULL,
      category_code TEXT NOT NULL,
      effective_from TEXT NOT NULL,
      personal_exemption_annual TEXT NOT NULL DEFAULT '0',
      insurance_floor TEXT NOT NULL DEFAULT '0',
      insurance_ceiling TEXT NOT NULL DEFAULT '0',
      employee_insurance_rate TEXT NOT NULL DEFAULT '0',
      employer_insurance_rate TEXT NOT NULL DEFAULT '0',
      enabled INTEGER NOT NULL DEFAULT 1,
      notes TEXT NOT NULL DEFAULT '',
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      UNIQUE(country_code,category_code,effective_from)
    );
    CREATE TABLE IF NOT EXISTS payroll_tax_brackets(
      id INTEGER PRIMARY KEY,
      rule_set_id INTEGER NOT NULL REFERENCES payroll_rule_sets(id) ON DELETE CASCADE,
      lower_bound TEXT NOT NULL,
      upper_bound TEXT,
      rate TEXT NOT NULL,
      UNIQUE(rule_set_id,lower_bound)
    );
    CREATE INDEX IF NOT EXISTS idx_payroll_rules_effective
      ON payroll_rule_sets(country_code,category_code,effective_from,enabled);
    """)
    employee_cols = {r["name"] for r in conn.execute("PRAGMA table_info(employees)")}
    for name, definition in {
        "legal_category": "TEXT NOT NULL DEFAULT 'local'",
        "insurance_number": "TEXT NOT NULL DEFAULT ''",
        "insured_wage": "TEXT NOT NULL DEFAULT '0'",
        "insurance_enabled": "INTEGER NOT NULL DEFAULT 1",
    }.items():
        if name not in employee_cols:
            conn.execute("ALTER TABLE employees ADD COLUMN " + name + " " + definition)
    line_cols = {r["name"] for r in conn.execute("PRAGMA table_info(payroll_lines)")}
    for name, definition in {
        "rule_set_id": "INTEGER REFERENCES payroll_rule_sets(id)",
        "taxable_annual": "TEXT NOT NULL DEFAULT '0'",
        "insured_wage": "TEXT NOT NULL DEFAULT '0'",
    }.items():
        if name not in line_cols:
            conn.execute("ALTER TABLE payroll_lines ADD COLUMN " + name + " " + definition)
    conn.commit()
    conn.close()


def current_tax_country(conn):
    row = conn.execute(
        "SELECT setting_value FROM company_settings WHERE setting_key='tax_country'"
    ).fetchone()
    return (row["setting_value"] if row else "EG").upper()


def _active_rule(conn, country_code, category_code, period):
    period_date = date.fromisoformat(period + "-01").isoformat()
    return conn.execute("""
        SELECT * FROM payroll_rule_sets
        WHERE country_code=? AND category_code=? AND enabled=1 AND effective_from<=?
        ORDER BY effective_from DESC,id DESC LIMIT 1
    """, (country_code, category_code, period_date)).fetchone()


def calculate_payroll_deductions(conn, employee, gross_monthly, period):
    """Return automatic monthly deductions, or None when no effective rule is configured."""
    country = current_tax_country(conn)
    category = (employee["legal_category"] or "local").strip().lower()
    rule = _active_rule(conn, country, category, period)
    if not rule:
        return None
    gross = _m(gross_monthly)
    insurance_enabled = int(employee["insurance_enabled"]) if "insurance_enabled" in employee.keys() else 1
    floor = _d(rule["insurance_floor"])
    ceiling = _d(rule["insurance_ceiling"])
    if ceiling > 0 and ceiling < floor:
        raise ValueError("الحد الأقصى لأجر التأمين أقل من الحد الأدنى في إعدادات القواعد.")
    if insurance_enabled:
        insured_wage = _d(employee["insured_wage"])
        if insured_wage <= 0:
            insured_wage = gross
        insured_wage = max(insured_wage, floor)
        if ceiling > 0:
            insured_wage = min(insured_wage, ceiling)
    else:
        insured_wage = Decimal("0")
    employee_rate = _d(rule["employee_insurance_rate"])
    employer_rate = _d(rule["employer_insurance_rate"])
    if min(employee_rate, employer_rate) < 0 or max(employee_rate, employer_rate) > 100:
        raise ValueError("نسبة التأمينات يجب أن تكون بين صفر و100.")
    employee_insurance = _m(insured_wage * employee_rate / Decimal("100"))
    employer_insurance = _m(insured_wage * employer_rate / Decimal("100"))
    exemption = _d(rule["personal_exemption_annual"])
    # Employee social insurance is deductible from the annual salary-tax base.
    annual_taxable = max(Decimal("0"), gross * Decimal("12") -
                         employee_insurance * Decimal("12") - exemption)
    brackets = conn.execute("""
        SELECT lower_bound,upper_bound,rate FROM payroll_tax_brackets
        WHERE rule_set_id=? ORDER BY CAST(lower_bound AS NUMERIC),id
    """, (rule["id"],)).fetchall()
    annual_tax = Decimal("0")
    if not brackets:
        raise ValueError("مجموعة قواعد الرواتب مفعّلة لكن لا توجد شرائح ضريبية. أضف الشرائح قبل إنشاء المسير.")
    if brackets:
        expected_lower = Decimal("0")
        for band in brackets:
            lower = _d(band["lower_bound"])
            upper = _d(band["upper_bound"]) if band["upper_bound"] not in (None, "") else None
            rate = _d(band["rate"])
            if lower != expected_lower or rate < 0 or rate > 100 or (upper is not None and upper <= lower):
                raise ValueError("شرائح ضريبة المرتبات غير متصلة أو تحتوي على نسبة/حد غير صحيح. راجع إعدادات قواعد الرواتب.")
            ceiling = upper if upper is not None else annual_taxable
            portion = max(Decimal("0"), min(annual_taxable, ceiling) - lower)
            annual_tax += portion * rate / Decimal("100")
            if upper is None:
                expected_lower = annual_taxable
                break
            expected_lower = upper
        if annual_taxable > expected_lower and (not brackets or brackets[-1]["upper_bound"] not in (None, "")):
            raise ValueError("شرائح ضريبة المرتبات لا تغطي كامل الدخل السنوي الخاضع للضريبة.")
    return {
        "rule_set_id": rule["id"],
        "tax": _m(annual_tax / Decimal("12")),
        "employee_insurance": employee_insurance,
        "employer_insurance": employer_insurance,
        "taxable_annual": _m(annual_taxable),
        "insured_wage": _m(insured_wage),
        "country_code": country,
        "category_code": category,
    }


def seed_egypt_2026(conn):
    """Insert a reviewable Egypt 2026 baseline; callers must explicitly request it."""
    conn.execute("""
        INSERT OR IGNORE INTO payroll_rule_sets(country_code,category_code,effective_from,
          personal_exemption_annual,insurance_floor,insurance_ceiling,
          employee_insurance_rate,employer_insurance_rate,notes)
        VALUES('EG','local','2026-01-01','20000','2700','16700','11','18.75',
          'إعداد مبدئي لمصر 2026؛ راجع المعاملة القانونية قبل الاعتماد')
    """)
    rule = conn.execute("""
        SELECT id FROM payroll_rule_sets
        WHERE country_code='EG' AND category_code='local' AND effective_from='2026-01-01'
    """).fetchone()
    if not rule:
        raise ValueError("تعذر إنشاء مجموعة قواعد مصر 2026.")
    conn.executemany("""
        INSERT OR IGNORE INTO payroll_tax_brackets(rule_set_id,lower_bound,upper_bound,rate)
        VALUES(?,?,?,?)
    """, [
        (rule["id"],"0","40000","0"),
        (rule["id"],"40000","55000","10"),
        (rule["id"],"55000","70000","15"),
        (rule["id"],"70000","200000","20"),
        (rule["id"],"200000","400000","22.5"),
        (rule["id"],"400000","1200000","25"),
        (rule["id"],"1200000",None,"27.5"),
    ])
    return rule["id"]


def _authorized(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        permissions = set(session.get("permissions", []))
        required = "workforce.manage" if request.method == "POST" else "workforce.view"
        fallback = {"admin", "accountant"} if required == "workforce.manage" else {"admin", "accountant", "viewer"}
        if required not in permissions and session.get("role") not in fallback:
            flash("لا تملك صلاحية عرض أو إدارة قواعد الرواتب.", "error")
            return redirect(url_for("dashboard"))
        return fn(*args, **kwargs)
    return wrapped


@payroll_compliance_bp.route("/hr/payroll-rules", methods=["GET", "POST"])
@_authorized
def rules():
    conn = _db()
    try:
        if request.method == "POST":
            action = request.form.get("action")
            if action == "seed_egypt_2026":
                rule_id = seed_egypt_2026(conn)
                _audit(conn, "payroll_rule_preset_loaded", "EG/local/2026-01-01 id=" + str(rule_id))
                conn.commit()
                flash("تم تحميل إعداد مصر 2026 للفئة local. راجع القواعد والشرائح قبل تشغيل أول مسير.", "success")
            elif action == "create_rule":
                country = request.form.get("country_code", "").strip().upper()
                category = request.form.get("category_code", "").strip().lower()
                effective = date.fromisoformat(request.form.get("effective_from", "")).isoformat()
                exemption = _m(request.form.get("personal_exemption_annual"))
                floor = _m(request.form.get("insurance_floor"))
                ceiling = _m(request.form.get("insurance_ceiling"))
                employee_rate = _d(request.form.get("employee_insurance_rate"))
                employer_rate = _d(request.form.get("employer_insurance_rate"))
                if len(country) != 2 or not category or min(exemption, floor, ceiling) < 0:
                    raise ValueError("أدخل رمز دولة من حرفين وفئة موظف وحدودًا غير سالبة.")
                if min(employee_rate, employer_rate) < 0 or max(employee_rate, employer_rate) > 100:
                    raise ValueError("نسب التأمينات يجب أن تكون بين صفر و100.")
                if ceiling and ceiling < floor:
                    raise ValueError("الحد الأقصى لأجر التأمين يجب ألا يقل عن الحد الأدنى.")
                cur = conn.execute("""
                    INSERT INTO payroll_rule_sets(country_code,category_code,effective_from,
                      personal_exemption_annual,insurance_floor,insurance_ceiling,
                      employee_insurance_rate,employer_insurance_rate,notes)
                    VALUES(?,?,?,?,?,?,?,?,?)
                """, (country,category,effective,str(exemption),str(floor),str(ceiling),
                      str(employee_rate),str(employer_rate),request.form.get("notes","").strip()))
                rule_id = cur.lastrowid
                _audit(conn, "payroll_rule_created", country + "/" + category + " id=" + str(rule_id))
                conn.commit()
                flash("تم إنشاء مجموعة القواعد. أضف شرائح ضريبة الدخل السنوية لها.", "success")
            elif action == "add_bracket":
                rule_id = int(request.form.get("rule_set_id"))
                lower = _m(request.form.get("lower_bound"))
                upper_raw = request.form.get("upper_bound", "").strip()
                upper = _m(upper_raw) if upper_raw else None
                rate = _d(request.form.get("rate"))
                if lower < 0 or (upper is not None and upper <= lower) or rate < 0 or rate > 100:
                    raise ValueError("راجع بداية/نهاية الشريحة ونسبة الضريبة.")
                rule = conn.execute("SELECT id FROM payroll_rule_sets WHERE id=?", (rule_id,)).fetchone()
                if not rule:
                    raise ValueError("مجموعة القواعد غير موجودة.")
                existing = conn.execute("SELECT lower_bound,upper_bound FROM payroll_tax_brackets WHERE rule_set_id=? ORDER BY CAST(lower_bound AS NUMERIC)", (rule_id,)).fetchall()
                expected = _d(existing[-1]["upper_bound"]) if existing and existing[-1]["upper_bound"] else (Decimal("0") if not existing else None)
                if expected is None or lower != expected:
                    raise ValueError("ابدأ أول شريحة من صفر، واجعل بداية كل شريحة مساوية لنهاية الشريحة السابقة.")
                if existing and existing[-1]["upper_bound"] in (None, ""):
                    raise ValueError("لا يمكن إضافة شريحة بعد شريحة مفتوحة النهاية.")
                conn.execute("INSERT INTO payroll_tax_brackets(rule_set_id,lower_bound,upper_bound,rate) VALUES(?,?,?,?)",
                             (rule_id,str(lower),str(upper) if upper is not None else None,str(rate)))
                _audit(conn, "payroll_tax_bracket_created", "rule=" + str(rule_id) + " from=" + str(lower))
                conn.commit()
                flash("تم حفظ الشريحة الضريبية.", "success")
            else:
                raise ValueError("نوع العملية غير معروف.")
    except (ValueError, KeyError, sqlite3.Error) as exc:
        conn.rollback()
        flash(str(exc) or "تعذر حفظ قواعد الرواتب.", "error")
    country = current_tax_country(conn)
    rule_sets = conn.execute("""
        SELECT r.*, (SELECT COUNT(*) FROM payroll_tax_brackets b WHERE b.rule_set_id=r.id) bracket_count
        FROM payroll_rule_sets r ORDER BY country_code,category_code,effective_from DESC
    """).fetchall()
    brackets = conn.execute("""
        SELECT b.*,r.country_code,r.category_code FROM payroll_tax_brackets b
        JOIN payroll_rule_sets r ON r.id=b.rule_set_id
        ORDER BY r.id,CAST(b.lower_bound AS NUMERIC)
    """).fetchall()
    conn.close()
    return render_template("payroll_rules.html", rule_sets=rule_sets, brackets=brackets,
                           current_country=country, today=date.today().isoformat())


def _audit(conn, action, details):
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='audit_log'").fetchone():
        conn.execute("INSERT INTO audit_log(user_id,action,details,created_at) VALUES(?,?,?,CURRENT_TIMESTAMP)",
                     (session.get("user_id"), action, details))


@payroll_compliance_bp.route("/hr/payroll-compliance")
@_authorized
def report():
    period = request.args.get("period", date.today().strftime("%Y-%m"))
    try:
        date.fromisoformat(period + "-01")
    except ValueError:
        period = date.today().strftime("%Y-%m")
    conn = _db()
    rows = conn.execute("""
        SELECT l.*,e.employee_code,e.full_name,e.legal_category,r.status
        FROM payroll_lines l
        JOIN payroll_runs r ON r.id=l.run_id
        JOIN employees e ON e.id=l.employee_id
        WHERE r.period=? AND r.status='posted'
        ORDER BY e.employee_code
    """, (period,)).fetchall()
    totals = {
        "gross": sum((_d(x["gross_pay"]) for x in rows), Decimal("0")),
        "net": sum((_d(x["net_pay"]) for x in rows), Decimal("0")),
        "tax": sum((_d(x["taxable_income_tax"]) for x in rows), Decimal("0")),
        "employee_insurance": sum((_d(x["employee_insurance"]) for x in rows), Decimal("0")),
        "employer_insurance": sum((_d(x["employer_insurance"]) for x in rows), Decimal("0")),
    }
    conn.close()
    return render_template("payroll_compliance_report.html", period=period, rows=rows, totals=totals)
