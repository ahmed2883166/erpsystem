from database import connect_database
"""Employee master, payroll preparation and employee custody workflows."""
import sqlite3
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import wraps
from pathlib import Path

from flask import Blueprint, flash, redirect, render_template, request, session, url_for
from accounting_core import post_journal

workforce_bp = Blueprint("workforce", __name__)
DB_PATH = Path(__file__).resolve().parent / "data" / "nexus.db"
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
      overtime_rate_percent TEXT NOT NULL DEFAULT '150'
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
    CREATE INDEX IF NOT EXISTS idx_payroll_period ON payroll_runs(period);
    CREATE INDEX IF NOT EXISTS idx_custody_employee ON employee_custodies(employee_id,status);
    """)
    employee_cols = {r["name"] for r in c.execute("PRAGMA table_info(employees)")}
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
    )


@workforce_bp.route("/hr", methods=["GET", "POST"])
@_role("admin", "accountant")
def employees():
    c = _db()
    if request.method == "POST":
        try:
            values = _employee_form_values(request.form)
            c.execute("""INSERT INTO employees(employee_code,full_name,job_title,department,hire_date,basic_salary,
                standard_hours,legal_category,insurance_number,insured_wage,insurance_enabled,address,national_id,
                work_status,raise_type,raise_value,raise_mode,raise_effective_date,overtime_rate_percent)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", values)
            _audit(c, "employee_created", values[0])
            c.commit()
            flash("تم إضافة الموظف وحفظ بياناته الوظيفية والأجرية.", "success")
        except (ValueError, KeyError, sqlite3.IntegrityError) as e:
            c.rollback()
            flash(str(e) or "كود الموظف أو الرقم القومي مستخدم بالفعل.", "error")
        c.close()
        return redirect(url_for("workforce.employees"))
    rows = c.execute("SELECT * FROM employees ORDER BY employee_code").fetchall()
    items = c.execute("""SELECT p.*,e.employee_code,e.full_name FROM employee_pay_items p
                         JOIN employees e ON e.id=p.employee_id ORDER BY e.employee_code,p.id DESC""").fetchall()
    incentive_rules = c.execute("SELECT * FROM payroll_incentive_rules ORDER BY active DESC,name").fetchall()
    c.close()
    return render_template("employees.html", employees=rows, items=items,
                           incentive_rules=incentive_rules)


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
            c.execute("""UPDATE employees SET employee_code=?,full_name=?,job_title=?,department=?,hire_date=?,
                basic_salary=?,standard_hours=?,legal_category=?,insurance_number=?,insured_wage=?,insurance_enabled=?,
                address=?,national_id=?,work_status=?,raise_type=?,raise_value=?,raise_mode=?,
                raise_effective_date=?,overtime_rate_percent=? WHERE id=?""", (*values, employee_id))
            _audit(c, "employee_updated", values[0])
            c.commit()
            flash("تم تحديث ملف الموظف.", "success")
            c.close()
            return redirect(url_for("workforce.employees"))
        except (ValueError, KeyError, sqlite3.IntegrityError) as exc:
            c.rollback()
            flash(str(exc) or "تعذر تحديث بيانات الموظف.", "error")
    c.close()
    return render_template("employee_edit.html", e=employee)


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
                overtime_rate = D(request.form.get("overtime_rate_percent_" + str(e["id"]), e["overtime_rate_percent"]))
                overtime_raw = request.form.get("overtime_amount_" + str(e["id"]), "").strip()
                if overtime_rate < 0 or overtime_rate > 500:
                    raise ValueError("نسبة الإضافي يجب أن تكون بين صفر و500.")
                overtime_amount = (M(D(overtime_hours) * basic_salary / D(e["standard_hours"]) * overtime_rate / Decimal("100"))
                                   if not overtime_raw else M(overtime_raw))
                absent_hours = D(request.form.get("absent_hours_" + str(e["id"]), "0"))
                absence = M(request.form.get("absence_deduction_" + str(e["id"]), "0"))
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
                advance_deduction = M(request.form.get("advance_" + str(e["id"]), "0"))
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
                    incentives,advance_deduction,total_entitlements,total_deductions,salary_raise,
                    overtime_rate_percent,incentive_rule_id,incentive_name,incentive_days,incentive_method)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (run_id,e["id"],str(basic_salary),str(allowances),str(overtime_hours),str(overtime_amount),
                     str(absent_hours),str(absence),str(other_deductions),str(tax),str(emp_ins),
                     str(employer_ins),str(gross),str(net),
                     rule_set_id, str(taxable_annual), str(insured_wage),
                     str(incentives),str(advance_deduction),str(total_entitlements),str(total_deductions),
                     str(raise_amount),str(overtime_rate),incentive_rule_id,incentive_name,
                     str(incentive_days),incentive_method))
            _audit(c, "payroll_draft_created", period)
            c.commit()
            flash("تم إنشاء مسير الرواتب كمسودة. راجع البنود ثم رحّله.", "success")
            return redirect(url_for("workforce.payroll_run", run_id=run_id))
        except (ValueError, InvalidOperation, KeyError, sqlite3.Error) as e:
            c.rollback()
            flash(str(e) or "تعذر إنشاء مسير الرواتب.", "error")
    employees = c.execute("""SELECT * FROM employees WHERE active=1
        AND work_status IN ('comprehensive_bonus','permanent') ORDER BY employee_code""").fetchall()
    incentive_rules = c.execute("SELECT * FROM payroll_incentive_rules WHERE active=1 ORDER BY name").fetchall()
    today_period = date.today().strftime("%Y-%m")
    runs = c.execute("SELECT * FROM payroll_runs ORDER BY period DESC LIMIT 24").fetchall()
    c.close()
    return render_template("payroll.html", employees=employees, runs=runs, today=today_period,
                           incentive_rules=incentive_rules)


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
    c.close()
    return render_template("payroll_run.html", run=run, rows=rows, accounts=accounts, totals=totals)


@workforce_bp.route("/hr/payroll/<int:run_id>/post", methods=["POST"])
@_role("admin", "accountant")
def post_payroll(run_id):
    c = _db()
    try:
        run = c.execute("SELECT * FROM payroll_runs WHERE id=?", (run_id,)).fetchone()
        if not run or run["status"] != "draft":
            raise ValueError("المسير غير موجود أو تم ترحيله بالفعل.")
        rows = c.execute("SELECT * FROM payroll_lines WHERE run_id=?", (run_id,)).fetchall()
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
        lines = [
            (salary_expense, M(gross), 0, "مصروف الأجور والمرتبات قبل الاستقطاعات"),
            (salary_expense, 0, M(absence_deduction), "تخفيض مصروف الأجور بخصم الغياب"),
            (employer_ins_expense, M(employer_ins), 0, "مصروف حصة الشركة في التأمينات"),
            (salaries_payable, 0, M(net), "صافي مستحقات الموظفين"),
            (advances_account, 0, M(advance_deduction), "استرداد سلف العاملين"),
            (other_deductions_account, 0, M(other_deductions), "الحساب المقابل للخصومات الأخرى"),
            (tax_payable, 0, M(tax), "ضريبة مرتبات مستحقة"),
            (insurance_payable, 0, M(emp_ins + employer_ins), "تأمينات اجتماعية مستحقة"),
        ]
        c.execute("BEGIN IMMEDIATE")
        journal_id = post_journal(c, run["period"] + "-01", "PAYROLL-" + run["period"],
                                  "مسير مرتبات شهر " + run["period"], lines, session["user_id"])
        c.execute("UPDATE payroll_runs SET status='posted',journal_entry_id=? WHERE id=?", (journal_id, run_id))
        _audit(c, "payroll_posted", run["period"] + " journal=" + str(journal_id))
        c.commit()
        flash("تم ترحيل مسير المرتبات وإنشاء القيد المحاسبي.", "success")
    except (ValueError, KeyError, sqlite3.Error) as e:
        c.rollback()
        flash(str(e), "error")
    c.close()
    return redirect(url_for("workforce.payroll_run", run_id=run_id))


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
    rows = c.execute("""SELECT x.*,e.employee_code,e.full_name FROM employee_custodies x
                        JOIN employees e ON e.id=x.employee_id ORDER BY x.id DESC LIMIT 100""").fetchall()
    employees = c.execute("SELECT id,employee_code,full_name FROM employees WHERE active=1 ORDER BY employee_code").fetchall()
    accounts = _accounts(c)
    c.close()
    return render_template("custody.html", rows=rows, employees=employees, accounts=accounts, today=date.today().isoformat())


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
