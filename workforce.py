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
    c = sqlite3.connect(DB_PATH, timeout=20, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    c.execute("PRAGMA busy_timeout=20000")
    return c


def initialize_workforce_schema():
    c = _db()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS employees(
      id INTEGER PRIMARY KEY, employee_code TEXT NOT NULL UNIQUE, full_name TEXT NOT NULL,
      job_title TEXT NOT NULL DEFAULT '', department TEXT NOT NULL DEFAULT '',
      hire_date TEXT NOT NULL, basic_salary TEXT NOT NULL, standard_hours TEXT NOT NULL DEFAULT '208',
      active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
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
      UNIQUE(run_id,employee_id)
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


@workforce_bp.route("/hr", methods=["GET", "POST"])
@_role("admin", "accountant")
def employees():
    c = _db()
    if request.method == "POST":
        try:
            salary = M(request.form.get("basic_salary"))
            hours = D(request.form.get("standard_hours", "208"))
            if salary < 0 or hours <= 0:
                raise ValueError("الراتب وساعات العمل القياسية يجب أن تكون قيمًا موجبة.")
            c.execute("INSERT INTO employees(employee_code,full_name,job_title,department,hire_date,basic_salary,standard_hours) VALUES(?,?,?,?,?,?,?)",
                (request.form["employee_code"].strip(), request.form["full_name"].strip(),
                 request.form.get("job_title","").strip(), request.form.get("department","").strip(),
                 request.form.get("hire_date") or date.today().isoformat(), str(salary), str(hours)))
            _audit(c, "employee_created", request.form["employee_code"].strip())
            c.commit()
            flash("تم إضافة الموظف.", "success")
        except (ValueError, KeyError, sqlite3.IntegrityError) as e:
            c.rollback()
            flash(str(e) if str(e) else "كود الموظف مستخدم بالفعل.", "error")
        c.close()
        return redirect(url_for("workforce.employees"))
    rows = c.execute("SELECT * FROM employees ORDER BY employee_code").fetchall()
    items = c.execute("""SELECT p.*,e.employee_code,e.full_name FROM employee_pay_items p
                         JOIN employees e ON e.id=p.employee_id ORDER BY e.employee_code,p.id DESC""").fetchall()
    c.close()
    return render_template("employees.html", employees=rows, items=items)


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
            employees = c.execute("SELECT * FROM employees WHERE active=1 ORDER BY employee_code").fetchall()
            for e in employees:
                items = c.execute("SELECT item_type,amount FROM employee_pay_items WHERE employee_id=? AND active=1", (e["id"],)).fetchall()
                allowances = sum((D(x["amount"]) for x in items if x["item_type"] == "allowance"), Decimal("0"))
                other_deductions = sum((D(x["amount"]) for x in items if x["item_type"] == "deduction"), Decimal("0"))
                overtime_hours = D(request.form.get("overtime_" + str(e["id"]), "0"))
                absent_hours = D(request.form.get("absent_" + str(e["id"]), "0"))
                tax = M(request.form.get("tax_" + str(e["id"]), "0"))
                emp_ins = M(request.form.get("employee_ins_" + str(e["id"]), "0"))
                employer_ins = M(request.form.get("employer_ins_" + str(e["id"]), "0"))
                standard = D(e["standard_hours"])
                if standard <= 0:
                    raise ValueError("ساعات العمل القياسية للموظف " + e["employee_code"] + " يجب أن تكون أكبر من صفر.")
                hourly = D(e["basic_salary"]) / standard
                overtime_amount = M(overtime_hours * hourly * D("1.5"))
                absence = M(absent_hours * hourly)
                gross = M(D(e["basic_salary"]) + allowances + overtime_amount - absence - other_deductions)
                net = M(gross - tax - emp_ins)
                if min(overtime_hours, absent_hours, tax, emp_ins, employer_ins, gross, net) < 0:
                    raise ValueError("لا يمكن أن تكون الساعات أو الاستقطاعات أو صافي المرتب سالبة.")
                c.execute("""INSERT INTO payroll_lines(run_id,employee_id,basic_salary,allowances,overtime_hours,
                    overtime_amount,absent_hours,absence_deduction,other_deductions,taxable_income_tax,
                    employee_insurance,employer_insurance,gross_pay,net_pay)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (run_id,e["id"],e["basic_salary"],str(M(allowances)),str(overtime_hours),str(overtime_amount),
                     str(absent_hours),str(absence),str(M(other_deductions)),str(tax),str(emp_ins),
                     str(employer_ins),str(gross),str(net)))
            _audit(c, "payroll_draft_created", period)
            c.commit()
            flash("تم إنشاء مسير الرواتب كمسودة. راجع البنود ثم رحّله.", "success")
            return redirect(url_for("workforce.payroll_run", run_id=run_id))
        except (ValueError, InvalidOperation, KeyError, sqlite3.Error) as e:
            c.rollback()
            flash(str(e) or "تعذر إنشاء مسير الرواتب.", "error")
    employees = c.execute("SELECT * FROM employees WHERE active=1 ORDER BY employee_code").fetchall()
    runs = c.execute("SELECT * FROM payroll_runs ORDER BY period DESC LIMIT 24").fetchall()
    c.close()
    return render_template("payroll.html", employees=employees, runs=runs, today=date.today().strftime("%Y-%m"))


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
    c.close()
    return render_template("payroll_run.html", run=run, rows=rows, accounts=accounts)


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
        lines = [
            (salary_expense, M(gross), 0, "مصروف الأجور والمرتبات"),
            (employer_ins_expense, M(employer_ins), 0, "حصة الشركة في التأمينات"),
            (salaries_payable, 0, M(net), "صافي مستحقات الموظفين"),
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
