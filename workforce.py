from app_paths import DB_PATH
from database import connect_database, fetch_page
"""Employee master, payroll preparation and employee custody workflows."""
import sqlite3
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import wraps

from flask import Blueprint, flash, redirect, render_template, request, session, url_for, g
from accounting_core import post_journal

workforce_bp = Blueprint("workforce", __name__)

CENT = Decimal("0.01")


def D(value):
    try:
        result = Decimal(str(value or "0"))
        if not result.is_finite():
            raise InvalidOperation()
        return result
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("يوجد مبلغ أو عدد ساعات غير صحيح.")


def M(value):
    return D(value).quantize(CENT, rounding=ROUND_HALF_UP)


def _db():
    return connect_database(DB_PATH, isolation_level=None)


def initialize_workforce_schema():
    c = _db()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS employees(
      id INTEGER PRIMARY KEY, employee_code TEXT NOT NULL UNIQUE, full_name TEXT NOT NULL,
      job_title TEXT NOT NULL DEFAULT '', department TEXT NOT NULL DEFAULT '',
      hire_date TEXT NOT NULL, basic_salary TEXT NOT NULL, standard_hours TEXT NOT NULL DEFAULT '208',
      active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      address TEXT NOT NULL DEFAULT '', national_id TEXT NOT NULL DEFAULT '',
      work_status TEXT NOT NULL DEFAULT 'permanent',
      raise_type TEXT NOT NULL DEFAULT 'fixed', raise_value TEXT NOT NULL DEFAULT '0',
      raise_mode TEXT NOT NULL DEFAULT 'basic', raise_effective_date TEXT NOT NULL DEFAULT '',
      overtime_rate_percent TEXT NOT NULL DEFAULT '150',
      branch_id INTEGER, cost_center_id INTEGER, project_id INTEGER
    );
    CREATE TABLE IF NOT EXISTS employee_pay_items(
      id INTEGER PRIMARY KEY, employee_id INTEGER NOT NULL REFERENCES employees(id),
      item_name TEXT NOT NULL, item_type TEXT NOT NULL CHECK(item_type IN ('allowance','deduction')),
      amount TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS payroll_runs(
      id INTEGER PRIMARY KEY, period TEXT NOT NULL UNIQUE, status TEXT NOT NULL DEFAULT 'draft',
      journal_entry_id INTEGER UNIQUE, created_by INTEGER, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS payroll_lines(
      id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES payroll_runs(id) ON DELETE CASCADE,
      employee_id INTEGER NOT NULL REFERENCES employees(id), basic_salary TEXT NOT NULL,
      allowances TEXT NOT NULL, overtime_hours TEXT NOT NULL, overtime_amount TEXT NOT NULL,
      absent_hours TEXT NOT NULL, absence_deduction TEXT NOT NULL, other_deductions TEXT NOT NULL,
      taxable_income_tax TEXT NOT NULL, employee_insurance TEXT NOT NULL,
      employer_insurance TEXT NOT NULL, gross_pay TEXT NOT NULL, net_pay TEXT NOT NULL,
      incentives TEXT NOT NULL DEFAULT '0', advance_deduction TEXT NOT NULL DEFAULT '0',
      total_entitlements TEXT NOT NULL DEFAULT '0', total_deductions TEXT NOT NULL DEFAULT '0',
      salary_raise TEXT NOT NULL DEFAULT '0', overtime_rate_percent TEXT NOT NULL DEFAULT '150',
      incentive_rule_id INTEGER, incentive_name TEXT NOT NULL DEFAULT '',
      incentive_days TEXT NOT NULL DEFAULT '0', incentive_method TEXT NOT NULL DEFAULT '',
      UNIQUE(run_id,employee_id)
    );
    CREATE TABLE IF NOT EXISTS payroll_payments(
      id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES payroll_runs(id),
      payment_date TEXT NOT NULL, amount TEXT NOT NULL,
      cash_account_id INTEGER NOT NULL REFERENCES accounts(id),
      journal_entry_id INTEGER NOT NULL UNIQUE, reference TEXT NOT NULL UNIQUE,
      notes TEXT NOT NULL DEFAULT '', created_by INTEGER,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_payroll_payments_run ON payroll_payments(run_id,payment_date,id);
    CREATE TABLE IF NOT EXISTS employee_loans (
      id INTEGER PRIMARY KEY, loan_no TEXT NOT NULL UNIQUE, employee_id INTEGER NOT NULL REFERENCES employees(id),
      request_date TEXT NOT NULL, requested_amount TEXT NOT NULL, approved_amount TEXT NOT NULL DEFAULT '0',
      installment_count INTEGER NOT NULL DEFAULT 1, first_due_period TEXT NOT NULL,
      remaining_balance TEXT NOT NULL DEFAULT '0',
      status TEXT NOT NULL DEFAULT 'requested' CHECK(status IN ('requested','approved','active','closed','rejected')),
      loan_account_id INTEGER REFERENCES accounts(id), disbursement_account_id INTEGER REFERENCES accounts(id),
      disbursement_date TEXT, journal_entry_id INTEGER UNIQUE, notes TEXT NOT NULL DEFAULT '',
      created_by INTEGER, approved_by INTEGER, approved_at TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS employee_loan_installments (
      id INTEGER PRIMARY KEY, loan_id INTEGER NOT NULL REFERENCES employee_loans(id),
      due_period TEXT NOT NULL, due_amount TEXT NOT NULL, paid_amount TEXT NOT NULL DEFAULT '0',
      status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','paid')), UNIQUE(loan_id,due_period)
    );
    CREATE TABLE IF NOT EXISTS payroll_loan_allocations (
      id INTEGER PRIMARY KEY, payroll_line_id INTEGER NOT NULL REFERENCES payroll_lines(id),
      installment_id INTEGER NOT NULL REFERENCES employee_loan_installments(id),
      amount TEXT NOT NULL, UNIQUE(payroll_line_id,installment_id)
    );
    CREATE TABLE IF NOT EXISTS payroll_incentive_rules(
      id INTEGER PRIMARY KEY, name TEXT NOT NULL,
      calculation_method TEXT NOT NULL CHECK(calculation_method IN ('fixed','percentage_basic','per_day')),
      calculation_value TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS employee_custodies(
      id INTEGER PRIMARY KEY, custody_no TEXT NOT NULL UNIQUE,
      employee_id INTEGER NOT NULL REFERENCES employees(id), issue_date TEXT NOT NULL,
      amount TEXT NOT NULL, remaining_amount TEXT NOT NULL, description TEXT NOT NULL,
      custody_account_id INTEGER NOT NULL REFERENCES accounts(id),
      cash_account_id INTEGER NOT NULL REFERENCES accounts(id),
      journal_entry_id INTEGER UNIQUE, status TEXT NOT NULL DEFAULT 'open',
      created_by INTEGER, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS custody_expenses(
      id INTEGER PRIMARY KEY, custody_id INTEGER NOT NULL REFERENCES employee_custodies(id),
      expense_date TEXT NOT NULL, expense_account_id INTEGER NOT NULL REFERENCES accounts(id),
      amount TEXT NOT NULL, description TEXT NOT NULL, receipt_reference TEXT NOT NULL DEFAULT '',
      created_by INTEGER, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS attendance_records(
      id INTEGER PRIMARY KEY, employee_id INTEGER NOT NULL REFERENCES employees(id),
      attendance_date TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('present','absent','permission','holiday')),
      check_in TEXT NOT NULL DEFAULT '', check_out TEXT NOT NULL DEFAULT '',
      absence_hours TEXT NOT NULL DEFAULT '0', approved_overtime_hours TEXT NOT NULL DEFAULT '0',
      notes TEXT NOT NULL DEFAULT '', recorded_by INTEGER, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      UNIQUE(employee_id,attendance_date)
    );
    CREATE INDEX IF NOT EXISTS idx_attendance_date ON attendance_records(attendance_date,employee_id);
    CREATE TABLE IF NOT EXISTS leave_types(
      id INTEGER PRIMARY KEY, code TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
      is_paid INTEGER NOT NULL DEFAULT 1, requires_balance INTEGER NOT NULL DEFAULT 0,
      active INTEGER NOT NULL DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS employee_leave_balances(
      id INTEGER PRIMARY KEY, employee_id INTEGER NOT NULL REFERENCES employees(id),
      leave_type_id INTEGER NOT NULL REFERENCES leave_types(id),
      balance_days TEXT NOT NULL DEFAULT '0', updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      UNIQUE(employee_id,leave_type_id)
    );
    CREATE TABLE IF NOT EXISTS employee_leave_requests(
      id INTEGER PRIMARY KEY, employee_id INTEGER NOT NULL REFERENCES employees(id),
      leave_type_id INTEGER NOT NULL REFERENCES leave_types(id),
      start_date TEXT NOT NULL, end_date TEXT NOT NULL, days TEXT NOT NULL,
      reason TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'requested'
        CHECK(status IN ('requested','approved','rejected')),
      requested_by INTEGER, decided_by INTEGER, decided_at TEXT,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_leave_requests_dates ON employee_leave_requests(start_date,end_date,status);
    CREATE INDEX IF NOT EXISTS idx_payroll_period ON payroll_runs(period);
    CREATE INDEX IF NOT EXISTS idx_custody_employee ON employee_custodies(employee_id,status);
    """)
    c.execute("INSERT OR IGNORE INTO leave_types(code,name,is_paid,requires_balance) VALUES('annual','إجازة سنوية',1,1)")
    c.execute("INSERT OR IGNORE INTO leave_types(code,name,is_paid,requires_balance) VALUES('unpaid','إجازة بدون أجر',0,0)")
    c.execute("INSERT OR IGNORE INTO leave_types(code,name,is_paid,requires_balance) VALUES('sick','إجازة مرضية',1,0)")
    employee_cols = {r["name"] for r in c.execute("PRAGMA table_info(employees)")}
    for name in ("branch_id", "cost_center_id", "project_id"):
        if name not in employee_cols:
            c.execute("ALTER TABLE employees ADD COLUMN " + name + " INTEGER")

    for name, definition in {
        "address": "TEXT NOT NULL DEFAULT ''",
        "national_id": "TEXT NOT NULL DEFAULT ''",
        "work_status": "TEXT NOT NULL DEFAULT 'permanent'",
        "raise_type": "TEXT NOT NULL DEFAULT 'fixed'",
        "raise_value": "TEXT NOT NULL DEFAULT '0'",
        "raise_mode": "TEXT NOT NULL DEFAULT 'basic'",
        "raise_effective_date": "TEXT NOT NULL DEFAULT ''",
        "overtime_rate_percent": "TEXT NOT NULL DEFAULT '150'",
    }.items():
        if name not in employee_cols:
            c.execute("ALTER TABLE employees ADD COLUMN " + name + " " + definition)
    payroll_cols = {r["name"] for r in c.execute("PRAGMA table_info(payroll_lines)")}
    for name, definition in {
        "loan_deduction": "TEXT NOT NULL DEFAULT '0'",
        "incentives": "TEXT NOT NULL DEFAULT '0'",
        "advance_deduction": "TEXT NOT NULL DEFAULT '0'",
        "total_entitlements": "TEXT NOT NULL DEFAULT '0'",
        "total_deductions": "TEXT NOT NULL DEFAULT '0'",
        "salary_raise": "TEXT NOT NULL DEFAULT '0'",
        "overtime_rate_percent": "TEXT NOT NULL DEFAULT '150'",
        "incentive_rule_id": "INTEGER",
        "incentive_name": "TEXT NOT NULL DEFAULT ''",
        "incentive_days": "TEXT NOT NULL DEFAULT '0'",
        "incentive_method": "TEXT NOT NULL DEFAULT ''",
    }.items():
        if name not in payroll_cols:
            c.execute("ALTER TABLE payroll_lines ADD COLUMN " + name + " " + definition)
    # Backfill legacy payroll runs without changing their original net amounts.
    legacy_rows = c.execute("""SELECT id,gross_pay,employer_insurance,employee_insurance,
        taxable_income_tax,advance_deduction,other_deductions,absence_deduction
        FROM payroll_lines WHERE total_entitlements='0' AND total_deductions='0'""").fetchall()
    for row in legacy_rows:
        employer = D(row["employer_insurance"])
        entitlements = M(D(row["gross_pay"]) + employer)
        deductions = M(employer + D(row["employee_insurance"]) + D(row["taxable_income_tax"])
                       + D(row["advance_deduction"]) + D(row["other_deductions"])
                       + D(row["absence_deduction"]))
        c.execute("UPDATE payroll_lines SET total_entitlements=?,total_deductions=? WHERE id=?",
                  (str(entitlements),str(deductions),row["id"]))
    c.close()


def _role(*roles):
    def deco(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            if "user_id" not in session:
                return redirect(url_for("login"))
            required = "workforce.manage" if request.method == "POST" else "workforce.view"
            if (required not in set(session["permissions"])) if "permissions" in session else (session.get("role") not in roles):
                flash("لا تملك صلاحية تنفيذ هذه العملية.", "error")
                return redirect(url_for("dashboard"))
            return fn(*args, **kwargs)
        return wrapped
    return deco


def _audit(c, action, details):
    if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='audit_log'").fetchone():
        c.execute("INSERT INTO audit_log(user_id,action,details,created_at) VALUES(?,?,?,CURRENT_TIMESTAMP)",
                  (session.get("user_id"), action, details))


def _accounts(c):
    return c.execute("SELECT id,code,name_ar,account_type FROM accounts WHERE active=1 AND is_group=0 ORDER BY code").fetchall()


def _dimension_options(c):
    tables = {row["name"] for row in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return {
        key: c.execute(f"SELECT id,code,name_ar FROM {table} WHERE active=1 ORDER BY code").fetchall()
        if table in tables else []
        for key, table in (("branches","branches"),("cost_centers","cost_centers"),("projects","projects"))
    }


def _validate_employee_dimensions(c, values):
    for value, table, label in zip(values[-3:], ("branches","cost_centers","projects"),
                                   ("الفرع","مركز التكلفة","المشروع")):
        if value is None:
            continue
        if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            raise ValueError("جدول " + label + " غير مهيأ.")
        if not c.execute(f"SELECT 1 FROM {table} WHERE id=? AND active=1", (value,)).fetchone():
            raise ValueError("اختر " + label + " نشطًا وصحيحًا.")


def _employee_form_values(form):
    salary = M(form.get("basic_salary"))
    hours = D(form.get("standard_hours", "208"))
    insured_wage = M(form.get("insured_wage", "0"))
    legal_category = form.get("legal_category", "local").strip().lower()
    work_status = form.get("work_status", "permanent").strip().lower()
    raise_type = form.get("raise_type", "fixed").strip().lower()
    raise_value = M(form.get("raise_value", "0"))
    raise_mode = form.get("raise_mode", "basic").strip().lower()
    overtime_rate = D(form.get("overtime_rate_percent", "150"))
    effective_date = form.get("raise_effective_date", "").strip()
    if effective_date:
        date.fromisoformat(effective_date)
    if (salary < 0 or hours <= 0 or insured_wage < 0 or not legal_category
        or work_status not in {"comprehensive_bonus", "permanent", "unpaid_leave", "resigned", "retired"}
        or raise_type not in {"fixed", "percentage"} or raise_mode not in {"basic", "allowance"}
        or raise_value < 0 or overtime_rate < 0 or overtime_rate > 500):
        raise ValueError("راجع الراتب وساعات العمل والحالة الوظيفية والعلاوة ونسبة الإضافي.")
    return (
        form["employee_code"].strip(), form["full_name"].strip(),
        form.get("job_title", "").strip(), form.get("department", "").strip(),
        form.get("hire_date") or date.today().isoformat(), str(salary), str(hours),
        legal_category, form.get("insurance_number", "").strip(), str(insured_wage),
        int(form.get("insurance_enabled", "1") == "1"),
        form.get("address", "").strip(), form.get("national_id", "").strip(),
        work_status, raise_type, str(raise_value), raise_mode, effective_date,
        str(overtime_rate),
        form.get("branch_id") or None, form.get("cost_center_id") or None,
        form.get("project_id") or None,
    )


@workforce_bp.route("/hr", methods=["GET", "POST"])
@_role("admin", "accountant")
def employees():
    c = _db()
    if request.method == "POST":
        try:
            values = _employee_form_values(request.form)
            _validate_employee_dimensions(c, values)
            c.execute("""INSERT INTO employees(employee_code,full_name,job_title,department,hire_date,basic_salary,
                standard_hours,legal_category,insurance_number,insured_wage,insurance_enabled,address,national_id,
                work_status,raise_type,raise_value,raise_mode,raise_effective_date,overtime_rate_percent,
                branch_id,cost_center_id,project_id)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", values)
            _audit(c, "employee_created", values[0])
            c.commit()
            flash("تم إضافة الموظف وحفظ بياناته الوظيفية والأجرية.", "success")
        except (ValueError, KeyError, sqlite3.IntegrityError) as e:
            c.rollback()
            flash(str(e) or "كود الموظف أو الرقم القومي مستخدم بالفعل.", "error")
        c.close()
        return redirect(url_for("workforce.employees"))
    employees_page = fetch_page(c, "SELECT * FROM employees ORDER BY employee_code",
        "SELECT COUNT(*) FROM employees",
        page=request.args.get("employees_page",1,type=int),
        per_page=request.args.get("employees_per_page",50,type=int))
    rows = employees_page["rows"]
    employee_options = c.execute("SELECT id,employee_code,full_name FROM employees ORDER BY employee_code").fetchall()
    items = c.execute("""SELECT p.*,e.employee_code,e.full_name FROM employee_pay_items p
                         JOIN employees e ON e.id=p.employee_id ORDER BY e.employee_code,p.id DESC""").fetchall()
    incentive_rules = c.execute("SELECT * FROM payroll_incentive_rules ORDER BY active DESC,name").fetchall()
    dimensions = _dimension_options(c)
    c.close()
    return render_template("employees.html", employees=rows, employee_options=employee_options,
                           employees_page=employees_page, items=items, incentive_rules=incentive_rules,
                           dimensions=dimensions)


@workforce_bp.route("/hr/<int:employee_id>/edit", methods=["GET", "POST"])
@_role("admin", "accountant")
def edit_employee(employee_id):
    c = _db()
    employee = c.execute("SELECT * FROM employees WHERE id=?", (employee_id,)).fetchone()
    if not employee:
        c.close()
        flash("الموظف غير موجود.", "error")
        return redirect(url_for("workforce.employees"))
    if request.method == "POST":
        try:
            values = _employee_form_values(request.form)
            _validate_employee_dimensions(c, values)
            c.execute("""UPDATE employees SET employee_code=?,full_name=?,job_title=?,department=?,hire_date=?,
                basic_salary=?,standard_hours=?,legal_category=?,insurance_number=?,insured_wage=?,insurance_enabled=?,
                address=?,national_id=?,work_status=?,raise_type=?,raise_value=?,raise_mode=?,
                raise_effective_date=?,overtime_rate_percent=?,branch_id=?,cost_center_id=?,project_id=? WHERE id=?""", (*values, employee_id))
            _audit(c, "employee_updated", values[0])
            c.commit()
            flash("تم تحديث ملف الموظف.", "success")
            c.close()
            return redirect(url_for("workforce.employees"))
        except (ValueError, KeyError, sqlite3.IntegrityError) as exc:
            c.rollback()
            flash(str(exc) or "تعذر تحديث بيانات الموظف.", "error")
    dimensions = _dimension_options(c)
    c.close()
    return render_template("employee_edit.html", e=employee, dimensions=dimensions)


@workforce_bp.route("/hr/incentive-rules", methods=["POST"])
@_role("admin", "accountant")
def add_incentive_rule():
    c = _db()
    try:
        name = request.form.get("name", "").strip()
        method = request.form.get("calculation_method", "")
        value = M(request.form.get("calculation_value", "0"))
        if not name or method not in {"fixed", "percentage_basic", "per_day"} or value <= 0:
            raise ValueError("أدخل اسم الحافز وطريقة حساب صحيحة وقيمة أكبر من صفر.")
        c.execute("""INSERT INTO payroll_incentive_rules(name,calculation_method,calculation_value)
                     VALUES(?,?,?)""", (name, method, str(value)))
        _audit(c, "payroll_incentive_rule_created", name)
        c.commit()
        flash("تم حفظ نوع الحافز ويمكن اختياره في مسير الرواتب.", "success")
    except (ValueError, sqlite3.Error) as exc:
        c.rollback()
        flash(str(exc) or "تعذر حفظ الحافز.", "error")
    c.close()
    return redirect(url_for("workforce.employees"))


@workforce_bp.route("/hr/pay-items", methods=["POST"])
@_role("admin", "accountant")
def add_pay_item():
    try:
        item_type = request.form.get("item_type")
        amount = M(request.form.get("amount"))
        if item_type not in {"allowance", "deduction"} or amount < 0:
            raise ValueError("نوع البند أو المبلغ غير صحيح.")
        c = _db()
        c.execute("INSERT INTO employee_pay_items(employee_id,item_name,item_type,amount) VALUES(?,?,?,?)",
                  (int(request.form["employee_id"]), request.form["item_name"].strip(), item_type, str(amount)))
        _audit(c, "employee_pay_item_created", request.form.get("item_name","").strip())
        c.commit()
        c.close()
        flash("تم حفظ البدل/الخصم الثابت.", "success")
    except (ValueError, KeyError, sqlite3.Error) as e:
        flash(str(e), "error")
    return redirect(url_for("workforce.employees"))


@workforce_bp.route("/hr/payroll", methods=["GET", "POST"])
@_role("admin", "accountant")
def payroll():
    c = _db()
    if request.method == "POST":
        period = request.form.get("period", "")
        try:
            start = date.fromisoformat(period + "-01")
            if period != start.strftime("%Y-%m"):
                raise ValueError()
            c.execute("BEGIN IMMEDIATE")
            cur = c.execute("INSERT INTO payroll_runs(period,status,created_by) VALUES(?,'draft',?)",
                            (period, session["user_id"]))
            run_id = cur.lastrowid
            employees = c.execute("""SELECT * FROM employees WHERE active=1
                AND work_status IN ('comprehensive_bonus','permanent') ORDER BY employee_code""").fetchall()
            if not employees:
                raise ValueError("لا يوجد موظفون مستحقون للراتب في هذا الشهر حسب الموقف الوظيفي.")
            for e in employees:
                # Every payroll amount is entered on the monthly form. Employee master
                # data and fixed pay items are not used to calculate this payroll.
                basic_raw = request.form.get("basic_" + str(e["id"]), "")
                if basic_raw is None or not str(basic_raw).strip():
                    raise ValueError("الراتب الأساسي للموظف " + e["full_name"] + " مطلوب ولا يجوز تركه فارغًا.")
                basic_salary = M(basic_raw)
                raise_amount = Decimal("0")
                if e["raise_effective_date"] and e["raise_effective_date"] <= (period + "-01"):
                    raise_amount = (M(basic_salary * D(e["raise_value"]) / Decimal("100"))
                                    if e["raise_type"] == "percentage" else M(e["raise_value"]))
                    if e["raise_mode"] == "basic":
                        basic_salary = M(basic_salary + raise_amount)
                if basic_salary <= 0:
                    raise ValueError("الراتب الأساسي للموظف " + e["full_name"] + " يجب أن يكون أكبر من صفر.")
                allowances = M(request.form.get("allowances_" + str(e["id"]), "0"))
                if raise_amount and e["raise_mode"] == "allowance":
                    allowances = M(allowances + raise_amount)
                overtime_hours = D(request.form.get("overtime_hours_" + str(e["id"]), "0"))
                if request.form.get("use_attendance_data") == "1":
                    attendance_rows = c.execute("""SELECT absence_hours,approved_overtime_hours
                        FROM attendance_records WHERE employee_id=? AND attendance_date>=? AND attendance_date<=?""",
                        (e["id"],period+"-01",(date(start.year + (start.month==12), 1 if start.month==12 else start.month+1, 1)-date.resolution).isoformat())).fetchall()
                    attendance_absent_hours = M(sum((D(x["absence_hours"]) for x in attendance_rows),Decimal("0")))
                    attendance_overtime_hours = M(sum((D(x["approved_overtime_hours"]) for x in attendance_rows),Decimal("0")))
                    overtime_hours = attendance_overtime_hours
                overtime_rate = D(request.form.get("overtime_rate_percent_" + str(e["id"]), e["overtime_rate_percent"]))
                overtime_raw = request.form.get("overtime_amount_" + str(e["id"]), "").strip()
                if overtime_rate < 0 or overtime_rate > 500:
                    raise ValueError("نسبة الإضافي يجب أن تكون بين صفر و500.")
                overtime_amount = (M(D(overtime_hours) * basic_salary / D(e["standard_hours"]) * overtime_rate / Decimal("100"))
                                   if not overtime_raw else M(overtime_raw))
                absent_hours = D(request.form.get("absent_hours_" + str(e["id"]), "0"))
                absence = M(request.form.get("absence_deduction_" + str(e["id"]), "0"))
                if request.form.get("use_attendance_data") == "1":
                    absent_hours = attendance_absent_hours
                    next_month = date(start.year + (start.month==12), 1 if start.month==12 else start.month+1, 1)
                    month_end = (next_month-date.resolution).isoformat()
                    unpaid_requests = c.execute("""SELECT r.start_date,r.end_date,r.days FROM employee_leave_requests r
                        JOIN leave_types t ON t.id=r.leave_type_id
                        WHERE r.employee_id=? AND r.status='approved' AND t.is_paid=0
                        AND r.start_date<=? AND r.end_date>=?""",(e["id"],month_end,period+"-01")).fetchall()
                    unpaid_days = Decimal("0")
                    for leave_row in unpaid_requests:
                        leave_start=date.fromisoformat(leave_row["start_date"])
                        leave_end=date.fromisoformat(leave_row["end_date"])
                        overlap_start=max(leave_start,start)
                        overlap_end=min(leave_end,date.fromisoformat(month_end))
                        total_days=Decimal((leave_end-leave_start).days+1)
                        overlap_days=Decimal((overlap_end-overlap_start).days+1)
                        unpaid_days += D(leave_row["days"])*overlap_days/total_days
                    # Payroll uses a transparent 30-day monthly divisor for approved unpaid leave.
                    absence = M(absent_hours*basic_salary/D(e["standard_hours"]) + unpaid_days*basic_salary/Decimal("30"))
                incentive_rule_id_raw = request.form.get("incentive_rule_" + str(e["id"]), "").strip()
                incentive_days = D(request.form.get("incentive_days_" + str(e["id"]), "0"))
                incentive_name = ""
                incentive_method = ""
                event_incentive = Decimal("0")
                incentive_rule_id = int(incentive_rule_id_raw) if incentive_rule_id_raw else None
                if incentive_rule_id:
                    incentive_rule = c.execute("SELECT * FROM payroll_incentive_rules WHERE id=? AND active=1",
                                                (incentive_rule_id,)).fetchone()
                    if not incentive_rule:
                        raise ValueError("نوع الحافز المختار غير موجود أو غير نشط.")
                    incentive_name = incentive_rule["name"]
                    incentive_method = incentive_rule["calculation_method"]
                    incentive_value = D(incentive_rule["calculation_value"])
                    if incentive_method == "fixed":
                        event_incentive = M(incentive_value)
                    elif incentive_method == "percentage_basic":
                        event_incentive = M(basic_salary * incentive_value / Decimal("100"))
                    else:
                        if incentive_days <= 0:
                            raise ValueError("أدخل عدد أيام الحافز للموظف " + e["full_name"] + ".")
                        event_incentive = M(incentive_value * incentive_days)
                incentives = M(M(request.form.get("incentives_" + str(e["id"]), "0")) + event_incentive)
                manual_advance_deduction = M(request.form.get("advance_" + str(e["id"]), "0"))
                due_installments = c.execute("""SELECT i.due_amount,i.paid_amount
                    FROM employee_loan_installments i JOIN employee_loans l ON l.id=i.loan_id
                    WHERE l.employee_id=? AND l.status='active' AND i.due_period<=? AND i.status='pending'
                    ORDER BY l.id,i.id""", (e["id"], period)).fetchall()
                loan_deduction = M(sum((max(Decimal("0"),D(i["due_amount"])-D(i["paid_amount"]))
                                         for i in due_installments),Decimal("0")))
                advance_deduction = M(manual_advance_deduction + loan_deduction)
                other_deductions = M(request.form.get("other_deductions_" + str(e["id"]), "0"))
                tax = M(request.form.get("tax_" + str(e["id"]), "0"))
                emp_ins = M(request.form.get("employee_ins_" + str(e["id"]), "0"))
                employer_ins = M(request.form.get("employer_ins_" + str(e["id"]), "0"))
                # Absence is a deduction, not part of gross wage.
                gross = M(basic_salary + allowances + overtime_amount + incentives)
                rule_set_id = None
                taxable_annual = Decimal("0")
                insured_wage = Decimal("0")
                if request.form.get("auto_calculate_statutory") == "1":
                    from payroll_compliance import calculate_payroll_deductions
                    automatic = calculate_payroll_deductions(
                        c, e, gross, period,
                        taxable_gross_monthly=M(max(Decimal("0"), gross - absence))
                    )
                    if automatic is None:
                        raise ValueError("لا توجد قواعد ضريبية وتأمينية سارية للموظف " + e["full_name"] + ". راجع إعدادات قواعد الرواتب أو ألغِ خيار الحساب التلقائي.")
                    tax = automatic["tax"]
                    emp_ins = automatic["employee_insurance"]
                    employer_ins = automatic["employer_insurance"]
                    rule_set_id = automatic["rule_set_id"]
                    taxable_annual = automatic["taxable_annual"]
                    insured_wage = automatic["insured_wage"]
                # Collect scheduled loan installments only up to the employee's available net pay.
                # Any unpaid remainder stays on the installment and rolls forward to later payrolls.
                fixed_deductions = M(employer_ins + emp_ins + tax + manual_advance_deduction + other_deductions + absence)
                available_for_loan = M(max(Decimal("0"),gross + employer_ins - fixed_deductions))
                loan_due_total = M(sum((max(Decimal("0"),D(i["due_amount"])-D(i["paid_amount"]))
                                        for i in due_installments),Decimal("0")))
                loan_deduction = min(loan_due_total,available_for_loan)
                advance_deduction = M(manual_advance_deduction + loan_deduction)
                total_entitlements = M(gross + employer_ins)
                total_deductions = M(employer_ins + emp_ins + tax + advance_deduction + other_deductions + absence)
                net = M(total_entitlements - total_deductions)
                if min(basic_salary, allowances, overtime_hours, overtime_amount, absent_hours,
                       absence, incentives, advance_deduction, other_deductions, tax, emp_ins,
                       employer_ins, gross, total_entitlements, total_deductions, net) < 0:
                    raise ValueError("لا يمكن أن تكون الأجور أو الساعات أو الاستقطاعات أو صافي المرتب سالبة.")
                c.execute("""INSERT INTO payroll_lines(run_id,employee_id,basic_salary,allowances,overtime_hours,
                    overtime_amount,absent_hours,absence_deduction,other_deductions,taxable_income_tax,
                    employee_insurance,employer_insurance,gross_pay,net_pay,rule_set_id,taxable_annual,insured_wage,
                    incentives,advance_deduction,loan_deduction,total_entitlements,total_deductions,salary_raise,
                    overtime_rate_percent,incentive_rule_id,incentive_name,incentive_days,incentive_method)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (run_id,e["id"],str(basic_salary),str(allowances),str(overtime_hours),str(overtime_amount),
                     str(absent_hours),str(absence),str(other_deductions),str(tax),str(emp_ins),
                     str(employer_ins),str(gross),str(net),
                     rule_set_id, str(taxable_annual), str(insured_wage),
                     str(incentives),str(advance_deduction),str(loan_deduction),str(total_entitlements),str(total_deductions),
                     str(raise_amount),str(overtime_rate),incentive_rule_id,incentive_name,
                     str(incentive_days),incentive_method))
            _audit(c, "payroll_draft_created", period)
            c.commit()
            g.audit_created_record = ("payroll_runs", run_id, "payroll_lines", "run_id")
            flash("تم إنشاء مسير الرواتب كمسودة. راجع البنود ثم رحّله.", "success")
            return redirect(url_for("workforce.payroll_run", run_id=run_id))
        except (ValueError, InvalidOperation, KeyError, sqlite3.Error) as e:
            c.rollback()
            flash(str(e) or "تعذر إنشاء مسير الرواتب.", "error")
    employees = c.execute("""SELECT * FROM employees WHERE active=1
        AND work_status IN ('comprehensive_bonus','permanent') ORDER BY employee_code""").fetchall()
    incentive_rules = c.execute("SELECT * FROM payroll_incentive_rules WHERE active=1 ORDER BY name").fetchall()
    today_period = date.today().strftime("%Y-%m")
    runs_page = fetch_page(c,"SELECT * FROM payroll_runs ORDER BY period DESC,id DESC",
        "SELECT COUNT(*) FROM payroll_runs",
        page=request.args.get("runs_page",1,type=int),
        per_page=request.args.get("runs_per_page",50,type=int))
    runs = runs_page["rows"]
    c.close()
    return render_template("payroll.html", employees=employees, runs=runs, runs_page=runs_page,
                           today=today_period, incentive_rules=incentive_rules)


@workforce_bp.route("/hr/payroll/<int:run_id>", methods=["GET"])
@_role("admin", "accountant", "viewer")
def payroll_run(run_id):
    c = _db()
    run = c.execute("SELECT * FROM payroll_runs WHERE id=?", (run_id,)).fetchone()
    if not run:
        c.close()
        flash("مسير الرواتب غير موجود.", "error")
        return redirect(url_for("workforce.payroll"))
    rows = c.execute("""SELECT l.*,e.employee_code,e.full_name,e.department FROM payroll_lines l
                        JOIN employees e ON e.id=l.employee_id WHERE l.run_id=? ORDER BY e.employee_code""", (run_id,)).fetchall()
    accounts = _accounts(c)
    payment_accounts = c.execute("SELECT id,code,name_ar,account_type FROM accounts WHERE active=1 AND is_group=0 AND account_type='asset' ORDER BY code").fetchall()
    payments = c.execute("""SELECT p.*,a.code AS account_code,a.name_ar AS account_name
        FROM payroll_payments p JOIN accounts a ON a.id=p.cash_account_id
        WHERE p.run_id=? ORDER BY p.payment_date,p.id""", (run_id,)).fetchall()
    paid_amount = M(sum((D(p["amount"]) for p in payments), Decimal("0")))
    totals = {
        "basic_salary": M(sum((D(x["basic_salary"]) for x in rows), Decimal("0"))),
        "allowances": M(sum((D(x["allowances"]) for x in rows), Decimal("0"))),
        "overtime_amount": M(sum((D(x["overtime_amount"]) for x in rows), Decimal("0"))),
        "absence_deduction": M(sum((D(x["absence_deduction"]) for x in rows), Decimal("0"))),
        "gross_pay": M(sum((D(x["gross_pay"]) for x in rows), Decimal("0"))),
        "salary_raise": M(sum((D(x["salary_raise"]) for x in rows), Decimal("0"))),
        "incentives": M(sum((D(x["incentives"]) for x in rows), Decimal("0"))),
        "advance_deduction": M(sum((D(x["advance_deduction"]) for x in rows), Decimal("0"))),
        "total_entitlements": M(sum((D(x["total_entitlements"]) for x in rows), Decimal("0"))),
        "total_deductions": M(sum((D(x["total_deductions"]) for x in rows), Decimal("0"))),
        "other_deductions": M(sum((D(x["other_deductions"]) for x in rows), Decimal("0"))),
        "tax": M(sum((D(x["taxable_income_tax"]) for x in rows), Decimal("0"))),
        "employee_insurance": M(sum((D(x["employee_insurance"]) for x in rows), Decimal("0"))),
        "employer_insurance": M(sum((D(x["employer_insurance"]) for x in rows), Decimal("0"))),
        "net_pay": M(sum((D(x["net_pay"]) for x in rows), Decimal("0"))),
    }
    remaining_payable = M(max(Decimal("0"), totals["net_pay"] - paid_amount))
    period_start = date.fromisoformat(run["period"] + "-01")
    next_month = date(period_start.year + (period_start.month == 12),
                      1 if period_start.month == 12 else period_start.month + 1, 1)
    period_end = (next_month - date.resolution).isoformat()
    c.close()
    return render_template("payroll_run.html", run=run, rows=rows, accounts=accounts,
                           payment_accounts=payment_accounts, payments=payments,
                           paid_amount=paid_amount, remaining_payable=remaining_payable,
                           today=date.today().isoformat(), totals=totals, period_end=period_end)


@workforce_bp.route("/hr/payroll/<int:run_id>/post", methods=["POST"])
@_role("admin", "accountant")
def post_payroll(run_id):
    c = _db()
    try:
        run = c.execute("SELECT * FROM payroll_runs WHERE id=?", (run_id,)).fetchone()
        if not run or run["status"] != "draft":
            raise ValueError("المسير غير موجود أو تم ترحيله بالفعل.")
        rows = c.execute("""SELECT l.*,e.branch_id,e.cost_center_id,e.project_id,e.full_name,e.employee_code
            FROM payroll_lines l JOIN employees e ON e.id=l.employee_id WHERE l.run_id=?
            ORDER BY e.employee_code""", (run_id,)).fetchall()
        if not rows:
            raise ValueError("لا توجد تفاصيل في مسير الرواتب.")
        salary_expense = int(request.form["salary_expense_account"])
        salaries_payable = int(request.form["salaries_payable_account"])
        tax_payable = int(request.form["tax_payable_account"])
        insurance_payable = int(request.form["insurance_payable_account"])
        employer_ins_expense = int(request.form["employer_ins_expense_account"])
        gross = sum((D(x["gross_pay"]) for x in rows), Decimal("0"))
        net = sum((D(x["net_pay"]) for x in rows), Decimal("0"))
        tax = sum((D(x["taxable_income_tax"]) for x in rows), Decimal("0"))
        emp_ins = sum((D(x["employee_insurance"]) for x in rows), Decimal("0"))
        employer_ins = sum((D(x["employer_insurance"]) for x in rows), Decimal("0"))
        other_deductions = sum((D(x["other_deductions"]) for x in rows), Decimal("0"))
        advance_deduction = sum((D(x["advance_deduction"]) for x in rows), Decimal("0"))
        absence_deduction = sum((D(x["absence_deduction"]) for x in rows), Decimal("0"))
        other_deductions_account = int(request.form["other_deductions_account"])
        advances_account = int(request.form["advances_account"])
        total_loan_deduction = M(sum((D(x["loan_deduction"]) for x in rows),Decimal("0")))
        if total_loan_deduction > 0:
            advance_account = c.execute("SELECT id,code,account_type,is_group,active FROM accounts WHERE id=?",
                                        (advances_account,)).fetchone()
            if not advance_account or advance_account["code"] != "1171" or advance_account["account_type"] != "asset" or advance_account["is_group"] or not advance_account["active"]:
                raise ValueError("خصم أقساط قروض الموظفين يتطلب حساب سلف العاملين 1171 نشطًا وتفصيليًا.")
        lines = []
        for payroll_line in rows:
            dims = {key: payroll_line[key] for key in ("branch_id", "cost_center_id", "project_id")}
            employee_label = payroll_line["employee_code"] + " - " + payroll_line["full_name"]
            gross_cost = M(payroll_line["gross_pay"])
            absence_cost = M(payroll_line["absence_deduction"])
            if gross_cost:
                lines.append((salary_expense, gross_cost, 0, "أجور ومرتبات - " + employee_label, dims))
            if absence_cost:
                lines.append((salary_expense, 0, absence_cost, "تخفيض مصروف الغياب - " + employee_label, dims))
            employer_cost = M(payroll_line["employer_insurance"])
            if employer_cost:
                lines.append((employer_ins_expense, employer_cost, 0, "حصة الشركة بالتأمينات - " + employee_label, dims))
        lines.extend([
            (salaries_payable, 0, M(net), "صافي مستحقات الموظفين"),
            (advances_account, 0, M(advance_deduction), "استرداد سلف العاملين"),
            (other_deductions_account, 0, M(other_deductions), "الحساب المقابل للخصومات الأخرى"),
            (tax_payable, 0, M(tax), "ضريبة مرتبات مستحقة"),
            (insurance_payable, 0, M(emp_ins + employer_ins), "تأمينات اجتماعية مستحقة"),
        ])
        c.execute("BEGIN IMMEDIATE")
        journal_id = post_journal(c, run["period"] + "-01", "PAYROLL-" + run["period"],
                                  "مسير مرتبات شهر " + run["period"], lines, session["user_id"])
        for payroll_line in rows:
            left = D(payroll_line["loan_deduction"])
            if left <= 0:
                continue
            due_rows = c.execute("""SELECT i.id,i.due_amount,i.paid_amount
                FROM employee_loan_installments i JOIN employee_loans l ON l.id=i.loan_id
                WHERE l.employee_id=(SELECT employee_id FROM payroll_lines WHERE id=?)
                  AND l.status='active' AND i.due_period=? AND i.status='pending'
                ORDER BY l.id,i.id""", (payroll_line["id"],run["period"])).fetchall()
            for item in due_rows:
                outstanding = max(Decimal("0"),D(item["due_amount"])-D(item["paid_amount"]))
                amount = min(left,outstanding)
                if amount <= 0:
                    continue
                c.execute("INSERT INTO payroll_loan_allocations(payroll_line_id,installment_id,amount) VALUES(?,?,?)",
                          (payroll_line["id"],item["id"],str(M(amount))))
                paid = M(D(item["paid_amount"])+amount)
                c.execute("UPDATE employee_loan_installments SET paid_amount=?,status=? WHERE id=?",
                          (str(paid),"paid" if paid>=D(item["due_amount"]) else "pending",item["id"]))
                left = M(left-amount)
                if left <= 0:
                    break
            if left > 0:
                raise ValueError("تعذر توزيع خصم قسط السلفة على جدول الأقساط.")
        for loan in c.execute("SELECT id,approved_amount FROM employee_loans WHERE status='active'").fetchall():
            paid = M(sum((D(x["paid_amount"]) for x in c.execute(
                "SELECT paid_amount FROM employee_loan_installments WHERE loan_id=?",(loan["id"],)).fetchall()),Decimal("0")))
            balance = M(max(Decimal("0"),D(loan["approved_amount"])-paid))
            c.execute("UPDATE employee_loans SET remaining_balance=?,status=? WHERE id=?",
                      (str(balance),"closed" if balance<=0 else "active",loan["id"]))
        c.execute("UPDATE payroll_runs SET status='posted',journal_entry_id=? WHERE id=?", (journal_id, run_id))
        _audit(c, "payroll_posted", run["period"] + " journal=" + str(journal_id))
        c.commit()
        flash("تم ترحيل مسير المرتبات وإنشاء القيد المحاسبي.", "success")
    except (ValueError, KeyError, sqlite3.Error) as e:
        c.rollback()
        flash(str(e), "error")
    c.close()
    return redirect(url_for("workforce.payroll_run", run_id=run_id))


@workforce_bp.route("/hr/payroll/<int:run_id>/payment", methods=["POST"])
@_role("admin", "accountant")
def pay_payroll(run_id):
    c = _db()
    try:
        run = c.execute("SELECT * FROM payroll_runs WHERE id=?", (run_id,)).fetchone()
        if not run or run["status"] != "posted":
            raise ValueError("لا يمكن صرف مرتب لمسير غير مرحّل.")
        rows = c.execute("SELECT net_pay FROM payroll_lines WHERE run_id=?", (run_id,)).fetchall()
        if not rows:
            raise ValueError("لا توجد تفاصيل في المسير.")
        net_payable = M(sum((D(row["net_pay"]) for row in rows), Decimal("0")))
        paid = M(sum((D(row["amount"]) for row in c.execute(
            "SELECT amount FROM payroll_payments WHERE run_id=?", (run_id,)).fetchall()), Decimal("0")))
        amount = M(request.form.get("amount"))
        if amount <= 0:
            raise ValueError("مبلغ صرف المرتب يجب أن يكون أكبر من صفر.")
        if amount > net_payable - paid:
            raise ValueError("مبلغ الصرف يتجاوز صافي المرتبات المتبقي.")
        payment_date = date.fromisoformat(request.form.get("payment_date") or date.today().isoformat()).isoformat()
        cash_account_id = int(request.form["cash_account_id"])
        account = c.execute("""SELECT id,code,account_type,is_group,active FROM accounts
            WHERE id=?""", (cash_account_id,)).fetchone()
        if not account or not account["active"] or account["is_group"] or account["account_type"] != "asset":
            raise ValueError("اختر حساب بنك أو صندوق تفصيليًا ونشطًا من الأصول.")
        reference = "PAYROLL-PAY-" + str(run_id) + "-" + datetime.now().strftime("%Y%m%d%H%M%S%f")
        c.execute("BEGIN IMMEDIATE")
        # Recheck outstanding value inside the write transaction to prevent concurrent overpayment.
        paid_now = M(sum((D(row["amount"]) for row in c.execute(
            "SELECT amount FROM payroll_payments WHERE run_id=?", (run_id,)).fetchall()), Decimal("0")))
        if amount > net_payable - paid_now:
            raise ValueError("مبلغ الصرف يتجاوز صافي المرتبات المتبقي بعد احتساب الدفعات الأخرى.")
        journal_id = post_journal(c, payment_date, reference,
            "صرف مرتبات مسير " + run["period"],
            [(int(c.execute("""SELECT jl.account_id FROM journal_lines jl
                WHERE jl.entry_id=? AND jl.memo='صافي مستحقات الموظفين' AND jl.credit>0
                ORDER BY jl.id LIMIT 1""", (run["journal_entry_id"],)).fetchone()[0]),
              amount, 0, "إقفال صافي مستحقات الموظفين"),
             (cash_account_id, 0, amount, "صرف صافي المرتبات من البنك/الصندوق")],
            session["user_id"])
        c.execute("""INSERT INTO payroll_payments(run_id,payment_date,amount,cash_account_id,
            journal_entry_id,reference,notes,created_by) VALUES(?,?,?,?,?,?,?,?)""",
            (run_id,payment_date,str(amount),cash_account_id,journal_id,reference,
             request.form.get("notes","").strip(),session["user_id"]))
        _audit(c, "payroll_payment_posted", reference + " amount=" + str(amount))
        c.commit()
        flash("تم صرف دفعة المرتبات وترحيل قيدها بنجاح.", "success")
    except (ValueError, KeyError, sqlite3.Error) as exc:
        c.rollback()
        flash(str(exc), "error")
    c.close()
    return redirect(url_for("workforce.payroll_run", run_id=run_id))


@workforce_bp.route("/hr/loans", methods=["GET", "POST"])
@_role("admin", "accountant")
def employee_loans():
    c = _db()
    if request.method == "POST":
        try:
            employee_id = int(request.form["employee_id"])
            amount = M(request.form.get("requested_amount", "0"))
            count = int(request.form.get("installment_count", "0"))
            first = request.form.get("first_due_period", "")
            date.fromisoformat(first + "-01")
            if not c.execute("SELECT 1 FROM employees WHERE id=? AND active=1", (employee_id,)).fetchone() or amount <= 0 or not 1 <= count <= 60:
                raise ValueError("راجع الموظف ومبلغ السلفة وعدد الأقساط من 1 إلى 60.")
            loan_no = "EMP-LOAN-" + datetime.now().strftime("%Y%m%d%H%M%S%f")
            c.execute("""INSERT INTO employee_loans(loan_no,employee_id,request_date,requested_amount,
                installment_count,first_due_period,notes,created_by) VALUES(?,?,?,?,?,?,?,?)""",
                (loan_no,employee_id,date.today().isoformat(),str(amount),count,first,
                 request.form.get("notes","").strip(),session["user_id"]))
            _audit(c,"employee_loan_requested",loan_no)
            c.commit()
            flash("تم تسجيل طلب السلفة في انتظار الموافقة.","success")
        except (ValueError,KeyError,sqlite3.Error) as exc:
            c.rollback()
            flash(str(exc) or "تعذر تسجيل طلب السلفة.","error")
        c.close()
        return redirect(url_for("workforce.employee_loans"))
    loans = c.execute("""SELECT l.*,e.employee_code,e.full_name FROM employee_loans l
        JOIN employees e ON e.id=l.employee_id ORDER BY l.id DESC""").fetchall()
    employees = c.execute("SELECT id,employee_code,full_name FROM employees WHERE active=1 ORDER BY employee_code").fetchall()
    cash_accounts = c.execute("""SELECT id,code,name_ar FROM accounts WHERE active=1 AND is_group=0
        AND account_type='asset' ORDER BY code""").fetchall()
    c.close()
    return render_template("employee_loans.html",loans=loans,employees=employees,
        cash_accounts=cash_accounts,today=date.today().strftime("%Y-%m"))


@workforce_bp.route("/hr/loans/<int:loan_id>/approve", methods=["POST"])
@_role("admin", "accountant")
def approve_employee_loan(loan_id):
    c = _db()
    try:
        loan = c.execute("SELECT * FROM employee_loans WHERE id=?", (loan_id,)).fetchone()
        if not loan or loan["status"] != "requested":
            raise ValueError("طلب السلفة غير موجود أو سبق اتخاذ قرار.")
        amount = M(request.form.get("approved_amount",loan["requested_amount"]))
        count = int(request.form.get("installment_count",loan["installment_count"]))
        first = request.form.get("first_due_period",loan["first_due_period"])
        date.fromisoformat(first + "-01")
        if amount <= 0 or amount > D(loan["requested_amount"]) or not 1 <= count <= 60:
            raise ValueError("المبلغ المعتمد لا يتجاوز المطلوب وعدد الأقساط من 1 إلى 60.")
        c.execute("""UPDATE employee_loans SET approved_amount=?,installment_count=?,first_due_period=?,
            status='approved',approved_by=?,approved_at=CURRENT_TIMESTAMP WHERE id=?""",
            (str(amount),count,first,session["user_id"],loan_id))
        _audit(c,"employee_loan_approved",loan["loan_no"])
        c.commit()
        flash("تم اعتماد السلفة؛ يلزم صرفها لإنشاء جدول الأقساط.","success")
    except (ValueError,KeyError,sqlite3.Error) as exc:
        c.rollback()
        flash(str(exc) or "تعذر اعتماد الطلب.","error")
    c.close()
    return redirect(url_for("workforce.employee_loans"))


@workforce_bp.route("/hr/loans/<int:loan_id>/reject", methods=["POST"])
@_role("admin", "accountant")
def reject_employee_loan(loan_id):
    c = _db()
    try:
        loan = c.execute("SELECT * FROM employee_loans WHERE id=?", (loan_id,)).fetchone()
        if not loan or loan["status"] != "requested":
            raise ValueError("لا يمكن رفض الطلب في حالته الحالية.")
        c.execute("UPDATE employee_loans SET status='rejected',approved_by=?,approved_at=CURRENT_TIMESTAMP WHERE id=?",
                  (session["user_id"],loan_id))
        _audit(c,"employee_loan_rejected",loan["loan_no"])
        c.commit()
        flash("تم رفض الطلب مع الاحتفاظ بسجله.","success")
    except (ValueError,sqlite3.Error) as exc:
        c.rollback()
        flash(str(exc) or "تعذر رفض الطلب.","error")
    c.close()
    return redirect(url_for("workforce.employee_loans"))


@workforce_bp.route("/hr/loans/<int:loan_id>/disburse", methods=["POST"])
@_role("admin", "accountant")
def disburse_employee_loan(loan_id):
    c = _db()
    try:
        loan = c.execute("""SELECT l.*,e.full_name FROM employee_loans l
            JOIN employees e ON e.id=l.employee_id WHERE l.id=?""",(loan_id,)).fetchone()
        if not loan or loan["status"] != "approved":
            raise ValueError("لا يمكن صرف إلا سلفة معتمدة لم تُصرف.")
        asset = c.execute("SELECT id,account_type,is_group,active FROM accounts WHERE code='1171'").fetchone()
        if not asset or not asset["active"] or asset["is_group"] or asset["account_type"] != "asset":
            raise ValueError("حساب سلف العاملين 1171 غير موجود أو غير نشط أو غير تفصيلي.")
        cash_id = int(request.form["cash_account_id"])
        cash = c.execute("SELECT id,account_type,is_group,active FROM accounts WHERE id=?",(cash_id,)).fetchone()
        if not cash or not cash["active"] or cash["is_group"] or cash["account_type"] != "asset" or cash_id == asset["id"]:
            raise ValueError("اختر حساب بنك أو صندوق نشطًا وتفصيليًا.")
        disb_date = date.fromisoformat(request.form.get("disbursement_date") or date.today().isoformat()).isoformat()
        amount = M(loan["approved_amount"])
        cents = int(amount * 100)
        base, rem = divmod(cents, int(loan["installment_count"]))
        c.execute("BEGIN IMMEDIATE")
        journal = post_journal(c,disb_date,"EMP-LOAN-DISB-"+str(loan_id),
            "صرف سلفة للموظف "+loan["full_name"],
            [(int(asset["id"]),amount,0,"إثبات سلفة موظف"),(cash_id,0,amount,"صرف سلفة من البنك/الصندوق")],
            session["user_id"])
        first = date.fromisoformat(loan["first_due_period"] + "-01")
        for idx in range(int(loan["installment_count"])):
            month = first.year * 12 + first.month - 1 + idx
            period = f"{month//12:04d}-{month%12+1:02d}"
            due = Decimal(base + (1 if idx < rem else 0)) / 100
            c.execute("INSERT INTO employee_loan_installments(loan_id,due_period,due_amount) VALUES(?,?,?)",
                      (loan_id,period,str(M(due))))
        c.execute("""UPDATE employee_loans SET status='active',remaining_balance=?,loan_account_id=?,
            disbursement_account_id=?,disbursement_date=?,journal_entry_id=? WHERE id=?""",
            (str(amount),asset["id"],cash_id,disb_date,journal,loan_id))
        _audit(c,"employee_loan_disbursed",loan["loan_no"]+" journal="+str(journal))
        c.commit()
        flash("تم صرف السلفة وترحيل القيد وإنشاء جدول الأقساط.","success")
    except (ValueError,KeyError,sqlite3.Error) as exc:
        c.rollback()
        flash(str(exc) or "تعذر صرف السلفة.","error")
    c.close()
    return redirect(url_for("workforce.employee_loans"))


@workforce_bp.route("/hr/loans/<int:loan_id>/schedule", methods=["GET"])
@_role("admin", "accountant", "viewer")
def loan_schedule(loan_id):
    c = _db()
    loan = c.execute("""SELECT l.*,e.employee_code,e.full_name FROM employee_loans l
        JOIN employees e ON e.id=l.employee_id WHERE l.id=?""",(loan_id,)).fetchone()
    if not loan:
        c.close()
        flash("السلفة غير موجودة.","error")
        return redirect(url_for("workforce.employee_loans"))
    installments = c.execute("SELECT * FROM employee_loan_installments WHERE loan_id=? ORDER BY due_period",(loan_id,)).fetchall()
    c.close()
    return render_template("employee_loan_schedule.html",loan=loan,installments=installments)


@workforce_bp.route("/hr/attendance", methods=["GET", "POST"])
@_role("admin", "accountant")
def attendance():
    c = _db()
    selected_date = request.values.get("attendance_date", date.today().isoformat())
    try:
        date.fromisoformat(selected_date)
    except ValueError:
        selected_date = date.today().isoformat()
    if request.method == "POST":
        try:
            employee_id = int(request.form["employee_id"])
            status = request.form.get("status","present")
            if status not in {"present","absent","permission","holiday"}:
                raise ValueError("حالة الحضور غير صحيحة.")
            check_in = request.form.get("check_in","").strip()
            check_out = request.form.get("check_out","").strip()
            absence_hours = D(request.form.get("absence_hours","0"))
            overtime_hours = D(request.form.get("approved_overtime_hours","0"))
            if min(absence_hours,overtime_hours)<0 or absence_hours>24 or overtime_hours>24:
                raise ValueError("ساعات الغياب أو الإضافي خارج النطاق المسموح.")
            for tm in (check_in,check_out):
                if tm:
                    datetime.strptime(tm,"%H:%M")
            if check_in and check_out and check_out <= check_in:
                raise ValueError("وقت الانصراف يجب أن يكون بعد وقت الحضور في نفس اليوم.")
            if status=="absent" and absence_hours<=0:
                raise ValueError("حدد ساعات الغياب عند تسجيل يوم غياب.")
            if status!="absent" and absence_hours>0:
                raise ValueError("ساعات الغياب تسجل فقط مع حالة غياب.")
            if not c.execute("SELECT 1 FROM employees WHERE id=? AND active=1",(employee_id,)).fetchone():
                raise ValueError("الموظف غير موجود أو غير نشط.")
            c.execute("""INSERT INTO attendance_records(employee_id,attendance_date,status,check_in,check_out,
                absence_hours,approved_overtime_hours,notes,recorded_by)
                VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(employee_id,attendance_date) DO UPDATE SET status=excluded.status,
                check_in=excluded.check_in,check_out=excluded.check_out,absence_hours=excluded.absence_hours,
                approved_overtime_hours=excluded.approved_overtime_hours,notes=excluded.notes,
                recorded_by=excluded.recorded_by""",
                (employee_id,selected_date,status,check_in,check_out,str(M(absence_hours)),
                 str(M(overtime_hours)),request.form.get("notes","").strip(),session["user_id"]))
            _audit(c,"attendance_saved",str(employee_id)+" "+selected_date+" "+status)
            c.commit()
            flash("تم حفظ سجل الحضور والانصراف.","success")
        except (ValueError,KeyError,sqlite3.Error) as exc:
            c.rollback()
            flash(str(exc) or "تعذر حفظ سجل الحضور.","error")
        c.close()
        return redirect(url_for("workforce.attendance",attendance_date=selected_date))
    employees = c.execute("SELECT id,employee_code,full_name FROM employees WHERE active=1 ORDER BY employee_code").fetchall()
    records = c.execute("""SELECT a.*,e.employee_code,e.full_name FROM attendance_records a
        JOIN employees e ON e.id=a.employee_id WHERE a.attendance_date=? ORDER BY e.employee_code""",
        (selected_date,)).fetchall()
    c.close()
    return render_template("attendance.html",employees=employees,records=records,selected_date=selected_date)


@workforce_bp.route("/hr/leaves", methods=["GET", "POST"])
@_role("admin", "accountant")
def leaves():
    c = _db()
    if request.method == "POST":
        try:
            employee_id=int(request.form["employee_id"]); leave_type_id=int(request.form["leave_type_id"])
            start=date.fromisoformat(request.form["start_date"]); end=date.fromisoformat(request.form["end_date"])
            days=D(request.form.get("days","0"))
            leave_type=c.execute("SELECT * FROM leave_types WHERE id=? AND active=1",(leave_type_id,)).fetchone()
            if end<start or days<=0 or not leave_type:
                raise ValueError("راجع نوع الإجازة والتواريخ وعدد الأيام.")
            if not c.execute("SELECT 1 FROM employees WHERE id=? AND active=1",(employee_id,)).fetchone():
                raise ValueError("الموظف غير موجود أو غير نشط.")
            c.execute("""INSERT INTO employee_leave_requests(employee_id,leave_type_id,start_date,end_date,days,
                reason,requested_by) VALUES(?,?,?,?,?,?,?)""",
                (employee_id,leave_type_id,start.isoformat(),end.isoformat(),str(M(days)),
                 request.form.get("reason","").strip(),session["user_id"]))
            _audit(c,"leave_requested",str(employee_id)+" "+start.isoformat()+".."+end.isoformat())
            c.commit(); flash("تم تسجيل طلب الإجازة في انتظار الاعتماد.","success")
        except (ValueError,KeyError,sqlite3.Error) as exc:
            c.rollback(); flash(str(exc) or "تعذر تسجيل طلب الإجازة.","error")
        c.close(); return redirect(url_for("workforce.leaves"))
    employees=c.execute("SELECT id,employee_code,full_name FROM employees WHERE active=1 ORDER BY employee_code").fetchall()
    types=c.execute("SELECT * FROM leave_types WHERE active=1 ORDER BY id").fetchall()
    balances=c.execute("""SELECT b.*,e.employee_code,e.full_name,t.name leave_name
        FROM employee_leave_balances b JOIN employees e ON e.id=b.employee_id
        JOIN leave_types t ON t.id=b.leave_type_id ORDER BY e.employee_code,t.id""").fetchall()
    requests=c.execute("""SELECT r.*,e.employee_code,e.full_name,t.name leave_name,t.is_paid,t.requires_balance
        FROM employee_leave_requests r JOIN employees e ON e.id=r.employee_id
        JOIN leave_types t ON t.id=r.leave_type_id ORDER BY r.id DESC""").fetchall()
    c.close()
    return render_template("leaves.html",employees=employees,types=types,balances=balances,requests=requests)


@workforce_bp.route("/hr/leaves/balance", methods=["POST"])
@_role("admin", "accountant")
def set_leave_balance():
    c=_db()
    try:
        employee_id=int(request.form["employee_id"]); leave_type_id=int(request.form["leave_type_id"])
        days=M(request.form.get("balance_days","0"))
        if days<0 or not c.execute("SELECT 1 FROM employees WHERE id=?",(employee_id,)).fetchone() or not c.execute("SELECT 1 FROM leave_types WHERE id=? AND requires_balance=1",(leave_type_id,)).fetchone():
            raise ValueError("رصيد الإجازة غير صحيح.")
        c.execute("""INSERT INTO employee_leave_balances(employee_id,leave_type_id,balance_days)
            VALUES(?,?,?) ON CONFLICT(employee_id,leave_type_id) DO UPDATE SET
            balance_days=excluded.balance_days,updated_at=CURRENT_TIMESTAMP""",(employee_id,leave_type_id,str(days)))
        _audit(c,"leave_balance_adjusted",str(employee_id)+" type="+str(leave_type_id)+" balance="+str(days))
        c.commit(); flash("تم تحديث رصيد الإجازة بعد المراجعة.","success")
    except (ValueError,KeyError,sqlite3.Error) as exc:
        c.rollback(); flash(str(exc) or "تعذر تحديث رصيد الإجازة.","error")
    c.close(); return redirect(url_for("workforce.leaves"))


@workforce_bp.route("/hr/leaves/<int:request_id>/decision", methods=["POST"])
@_role("admin", "accountant")
def decide_leave(request_id):
    c=_db()
    try:
        c.execute("BEGIN IMMEDIATE")
        leave=c.execute("""SELECT r.*,t.is_paid,t.requires_balance,t.name leave_name
            FROM employee_leave_requests r JOIN leave_types t ON t.id=r.leave_type_id WHERE r.id=?""",
            (request_id,)).fetchone()
        decision=request.form.get("decision")
        if not leave or leave["status"]!="requested" or decision not in {"approved","rejected"}:
            raise ValueError("طلب الإجازة غير موجود أو سبق اتخاذ قرار.")
        if decision=="approved" and leave["is_paid"] and leave["requires_balance"]:
            balance=c.execute("SELECT balance_days FROM employee_leave_balances WHERE employee_id=? AND leave_type_id=?",
                (leave["employee_id"],leave["leave_type_id"])).fetchone()
            if not balance or D(balance["balance_days"])<D(leave["days"]):
                raise ValueError("رصيد الإجازة السنوية غير كافٍ.")
            new_balance=M(D(balance["balance_days"])-D(leave["days"]))
            c.execute("UPDATE employee_leave_balances SET balance_days=?,updated_at=CURRENT_TIMESTAMP WHERE employee_id=? AND leave_type_id=?",
                (str(new_balance),leave["employee_id"],leave["leave_type_id"]))
        c.execute("UPDATE employee_leave_requests SET status=?,decided_by=?,decided_at=CURRENT_TIMESTAMP WHERE id=?",
            (decision,session["user_id"],request_id))
        _audit(c,"leave_"+decision,str(request_id)+" "+leave["leave_name"])
        c.commit(); flash("تم اعتماد الإجازة." if decision=="approved" else "تم رفض الإجازة.","success")
    except (ValueError,KeyError,sqlite3.Error) as exc:
        c.rollback(); flash(str(exc) or "تعذر اتخاذ قرار الإجازة.","error")
    c.close(); return redirect(url_for("workforce.leaves"))


@workforce_bp.route("/hr/custody", methods=["GET", "POST"])
@_role("admin", "accountant")
def custody():
    c = _db()
    if request.method == "POST":
        try:
            amount = M(request.form.get("amount"))
            if amount <= 0:
                raise ValueError("قيمة العهدة يجب أن تكون أكبر من صفر.")
            employee_id = int(request.form["employee_id"])
            cash_id = int(request.form["cash_account_id"])
            custody_id = int(request.form["custody_account_id"])
            no = "CUST-" + datetime.now().strftime("%Y%m%d%H%M%S%f")
            c.execute("BEGIN IMMEDIATE")
            journal_id = post_journal(c, request.form.get("issue_date") or date.today().isoformat(), no,
                "صرف عهدة موظف", [(custody_id, amount, 0, "عهدة موظف"),
                                  (cash_id, 0, amount, "صرف العهدة من الصندوق/البنك")], session["user_id"])
            c.execute("""INSERT INTO employee_custodies(custody_no,employee_id,issue_date,amount,remaining_amount,
                description,custody_account_id,cash_account_id,journal_entry_id,created_by)
                VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (no,employee_id,request.form.get("issue_date") or date.today().isoformat(),str(amount),str(amount),
                 request.form.get("description","").strip(),custody_id,cash_id,journal_id,session["user_id"]))
            _audit(c, "employee_custody_issued", no + " amount=" + str(amount))
            c.commit()
            flash("تم صرف العهدة وترحيل قيدها.", "success")
        except (ValueError, KeyError, sqlite3.Error) as e:
            c.rollback()
            flash(str(e), "error")
        c.close()
        return redirect(url_for("workforce.custody"))
    custody_page = fetch_page(c,
        """SELECT x.*,e.employee_code,e.full_name FROM employee_custodies x
           JOIN employees e ON e.id=x.employee_id ORDER BY x.id DESC""",
        "SELECT COUNT(*) FROM employee_custodies",
        page=request.args.get("custody_page",1,type=int),
        per_page=request.args.get("custody_per_page",50,type=int))
    rows = custody_page["rows"]
    employees = c.execute("SELECT id,employee_code,full_name FROM employees WHERE active=1 ORDER BY employee_code").fetchall()
    accounts = _accounts(c)
    c.close()
    return render_template("custody.html", rows=rows, custody_page=custody_page, employees=employees, accounts=accounts, today=date.today().isoformat())


@workforce_bp.route("/hr/custody/<int:custody_id>/expense", methods=["POST"])
@_role("admin", "accountant")
def custody_expense(custody_id):
    c = _db()
    try:
        row = c.execute("SELECT * FROM employee_custodies WHERE id=? AND status='open'", (custody_id,)).fetchone()
        if not row:
            raise ValueError("العهدة غير موجودة أو مغلقة.")
        amount = M(request.form.get("amount"))
        used = sum((D(x["amount"]) for x in c.execute("SELECT amount FROM custody_expenses WHERE custody_id=?", (custody_id,)).fetchall()), Decimal("0"))
        if amount <= 0 or used + amount > D(row["amount"]):
            raise ValueError("قيمة المصروف تتجاوز المتاح في العهدة.")
        c.execute("INSERT INTO custody_expenses(custody_id,expense_date,expense_account_id,amount,description,receipt_reference,created_by) VALUES(?,?,?,?,?,?,?)",
            (custody_id,request.form.get("expense_date") or date.today().isoformat(),int(request.form["expense_account_id"]),
             str(amount),request.form.get("description","").strip(),request.form.get("receipt_reference","").strip(),session["user_id"]))
        _audit(c, "employee_custody_expense_added", str(custody_id) + " amount=" + str(amount))
        c.commit()
        flash("تم تسجيل مصروف العهدة بانتظار التسوية والاعتماد.", "success")
    except (ValueError, KeyError, sqlite3.Error) as e:
        c.rollback()
        flash(str(e), "error")
    c.close()
    return redirect(url_for("workforce.custody"))


@workforce_bp.route("/hr/custody/<int:custody_id>/settle", methods=["POST"])
@_role("admin", "accountant")
def settle_custody(custody_id):
    c = _db()
    try:
        row = c.execute("SELECT * FROM employee_custodies WHERE id=? AND status='open'", (custody_id,)).fetchone()
        if not row:
            raise ValueError("العهدة غير موجودة أو تمت تسويتها.")
        expenses = c.execute("SELECT * FROM custody_expenses WHERE custody_id=? ORDER BY id", (custody_id,)).fetchall()
        expense_total = sum((D(x["amount"]) for x in expenses), Decimal("0"))
        returned = M(request.form.get("returned_amount"))
        total = M(row["amount"])
        if returned < 0 or expense_total + returned != total:
            raise ValueError("لا بد أن يساوي مجموع المصروفات والمبلغ المرتد قيمة العهدة بالكامل.")
        lines = [(int(x["expense_account_id"]), M(x["amount"]), 0, x["description"] or "مصروف عهدة") for x in expenses]
        if returned:
            lines.append((int(row["cash_account_id"]), returned, 0, "رد المتبقي من العهدة"))
        lines.append((int(row["custody_account_id"]), 0, total, "تسوية عهدة الموظف"))
        c.execute("BEGIN IMMEDIATE")
        journal_id = post_journal(c, request.form.get("settlement_date") or date.today().isoformat(),
            "CUST-SETTLE-" + row["custody_no"], "تسوية عهدة " + row["custody_no"], lines, session["user_id"])
        c.execute("UPDATE employee_custodies SET remaining_amount='0',status='settled' WHERE id=?", (custody_id,))
        _audit(c, "employee_custody_settled", row["custody_no"] + " journal=" + str(journal_id))
        c.commit()
        flash("تمت تسوية العهدة بالكامل وترحيل القيد.", "success")
    except (ValueError, KeyError, sqlite3.Error) as e:
        c.rollback()
        flash(str(e), "error")
    c.close()
    return redirect(url_for("workforce.custody"))
