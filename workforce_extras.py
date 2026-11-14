"""Additive employee statement, penalty and payroll-allocation workflows.

All historical payroll and journal records are retained. This module only adds
new tables/columns and records snapshots for newly prepared payroll runs.
"""
import sqlite3
from pathlib import Path
from datetime import date
from decimal import Decimal

from flask import flash, redirect, render_template, request, session, url_for

import workforce
from backup_scheduler import create_backup_archive
from workforce import workforce_bp, _db, _role, D, M, _audit


def create_workforce_safety_backup():
    """Take one verified online SQLite snapshot before workforce schema upgrades."""
    db_path = Path(workforce.DB_PATH)
    if not db_path.exists() or db_path.stat().st_size == 0:
        return None
    check = sqlite3.connect(str(db_path))
    try:
        upgraded = check.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='employee_penalties'").fetchone()
    finally:
        check.close()
    if upgraded:
        return None
    marker = db_path.parent / ".nexus-workforce-schema-backup-v1"
    backup_dir = db_path.parent / "migration_backups"
    archive = create_backup_archive(db_path, backup_dir, keep_count=20)
    marker.write_text(archive.name, encoding="utf-8")
    return str(archive)


def initialize_workforce_extras_schema():
    c = _db()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS employee_penalties(
      id INTEGER PRIMARY KEY,
      employee_id INTEGER NOT NULL REFERENCES employees(id),
      penalty_date TEXT NOT NULL,
      effective_period TEXT NOT NULL,
      reason TEXT NOT NULL,
      amount TEXT NOT NULL,
      duration_days TEXT NOT NULL DEFAULT '0',
      reference TEXT NOT NULL DEFAULT '',
      status TEXT NOT NULL DEFAULT 'requested'
        CHECK(status IN ('requested','approved','rejected','deducted','cancelled')),
      requested_by INTEGER,
      decided_by INTEGER,
      decided_at TEXT,
      payroll_line_id INTEGER REFERENCES payroll_lines(id),
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_employee_penalties_period
      ON employee_penalties(employee_id,effective_period,status);
    CREATE TABLE IF NOT EXISTS payroll_line_items(
      id INTEGER PRIMARY KEY,
      payroll_line_id INTEGER NOT NULL REFERENCES payroll_lines(id),
      section TEXT NOT NULL CHECK(section IN ('earning','deduction','employer_cost','information')),
      item_name TEXT NOT NULL,
      amount TEXT NOT NULL,
      source_type TEXT NOT NULL DEFAULT 'manual',
      source_id INTEGER,
      sort_order INTEGER NOT NULL DEFAULT 0,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_payroll_line_items_line
      ON payroll_line_items(payroll_line_id,section,sort_order,id);
    CREATE TABLE IF NOT EXISTS payroll_payment_allocations(
      id INTEGER PRIMARY KEY,
      payment_id INTEGER NOT NULL REFERENCES payroll_payments(id),
      payroll_line_id INTEGER NOT NULL REFERENCES payroll_lines(id),
      amount TEXT NOT NULL,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      UNIQUE(payment_id,payroll_line_id)
    );
    CREATE INDEX IF NOT EXISTS idx_payroll_payment_alloc_line
      ON payroll_payment_allocations(payroll_line_id,payment_id);
    """)
    cols = {r["name"] for r in c.execute("PRAGMA table_info(payroll_lines)")}
    if "penalty_deduction" not in cols:
        c.execute("ALTER TABLE payroll_lines ADD COLUMN penalty_deduction TEXT NOT NULL DEFAULT '0'")
    # Preserve old payroll totals as aggregate snapshots. Do not pretend legacy
    # aggregated allowances/deductions contain item-level detail.
    migration = "payroll_line_items_legacy_snapshot_v1"
    if not c.execute("SELECT 1 FROM workforce_migrations WHERE migration_key=?", (migration,)).fetchone():
        rows = c.execute("""SELECT id,basic_salary,allowances,incentives,overtime_amount,
            absence_deduction,advance_deduction,other_deductions,taxable_income_tax,
            employee_insurance,employer_insurance FROM payroll_lines""").fetchall()
        for row in rows:
            line_id = row["id"]
            legacy = [
                ("earning","الراتب الأساسي","basic_salary"),
                ("information","العلاوة المطبقة (بيان تاريخي)","salary_raise"),
                ("earning","البدلات (إجمالي تاريخي)","allowances"),
                ("earning","الحوافز (إجمالي تاريخي)","incentives"),
                ("earning","الإضافي","overtime_amount"),
                ("deduction","خصم الغياب","absence_deduction"),
                ("deduction","السلف والدفعات المقدمة (إجمالي تاريخي)","advance_deduction"),
                ("deduction","الخصومات الأخرى (إجمالي تاريخي)","other_deductions"),
                ("deduction","ضريبة المرتبات","taxable_income_tax"),
                ("deduction","تأمين الموظف","employee_insurance"),
                ("employer_cost","تأمين الشركة","employer_insurance"),
            ]
            for section,label,key in legacy:
                amount = M(row[key] or "0")
                if amount:
                    c.execute("""INSERT INTO payroll_line_items
                        (payroll_line_id,section,item_name,amount,source_type)
                        VALUES(?,?,?,?, 'legacy_aggregate')""",
                        (line_id,section,label,str(amount)))
        c.execute("INSERT INTO workforce_migrations(migration_key) VALUES(?)",(migration,))
    c.close()


@workforce_bp.route("/hr/penalties", methods=["GET","POST"])
@_role("admin","accountant")
def employee_penalties():
    c = _db()
    if request.method == "POST":
        try:
            employee_id = int(request.form["employee_id"])
            penalty_date = date.fromisoformat(request.form["penalty_date"]).isoformat()
            period = request.form["effective_period"].strip()
            if date.fromisoformat(period + "-01").strftime("%Y-%m") != period:
                raise ValueError()
            amount = M(request.form.get("amount","0"))
            days = M(request.form.get("duration_days","0"))
            reason = request.form.get("reason","").strip()
            if not c.execute("SELECT 1 FROM employees WHERE id=?",(employee_id,)).fetchone():
                raise ValueError("الموظف غير موجود.")
            if not reason or amount < 0 or days < 0 or (amount == 0 and days == 0):
                raise ValueError("أدخل سبب الجزاء وقيمة أو مدة صحيحة.")
            c.execute("""INSERT INTO employee_penalties
                (employee_id,penalty_date,effective_period,reason,amount,duration_days,reference,requested_by)
                VALUES(?,?,?,?,?,?,?,?)""",
                (employee_id,penalty_date,period,reason,str(amount),str(days),
                 request.form.get("reference","").strip(),session["user_id"]))
            _audit(c,"employee_penalty_requested",f"{employee_id} {period} {amount}")
            c.commit()
            flash("تم تسجيل الجزاء في انتظار الاعتماد، ولم يُخصم من المرتب بعد.","success")
        except (ValueError,KeyError,sqlite3.Error) as exc:
            c.rollback()
            flash(str(exc) or "تعذر تسجيل الجزاء.","error")
        c.close()
        return redirect(url_for("workforce.employee_penalties"))
    employees = c.execute("SELECT id,employee_code,full_name FROM employees ORDER BY employee_code").fetchall()
    rows = c.execute("""SELECT p.*,e.employee_code,e.full_name
        FROM employee_penalties p JOIN employees e ON e.id=p.employee_id
        ORDER BY p.penalty_date DESC,p.id DESC""").fetchall()
    c.close()
    return render_template("employee_penalties.html",employees=employees,rows=rows,
                           today=date.today().isoformat(),period=date.today().strftime("%Y-%m"))


@workforce_bp.route("/hr/penalties/<int:penalty_id>/decision", methods=["POST"])
@_role("admin","accountant")
def decide_employee_penalty(penalty_id):
    c = _db()
    try:
        decision = request.form.get("decision")
        if decision not in {"approved","rejected","cancelled"}:
            raise ValueError("قرار الجزاء غير صحيح.")
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT * FROM employee_penalties WHERE id=?",(penalty_id,)).fetchone()
        if not row or row["status"] != "requested":
            raise ValueError("الجزاء غير موجود أو سبق اتخاذ قرار بشأنه.")
        c.execute("""UPDATE employee_penalties SET status=?,decided_by=?,decided_at=CURRENT_TIMESTAMP
            WHERE id=? AND status='requested'""",(decision,session["user_id"],penalty_id))
        _audit(c,"employee_penalty_"+decision,str(penalty_id))
        c.commit()
        flash("تم تحديث حالة الجزاء.","success")
    except (ValueError,sqlite3.Error) as exc:
        c.rollback()
        flash(str(exc),"error")
    c.close()
    return redirect(url_for("workforce.employee_penalties"))


@workforce_bp.route("/hr/employee/<int:employee_id>/statement", methods=["GET"])
@_role("admin","accountant","viewer")
def employee_financial_statement(employee_id):
    c = _db()
    employee = c.execute("SELECT * FROM employees WHERE id=?",(employee_id,)).fetchone()
    if not employee:
        c.close()
        flash("الموظف غير موجود.","error")
        return redirect(url_for("workforce.employees"))
    month_from = request.args.get("month_from","").strip()
    month_to = request.args.get("month_to","").strip()
    try:
        for value in (month_from,month_to):
            if value and date.fromisoformat(value+"-01").strftime("%Y-%m") != value:
                raise ValueError()
    except ValueError:
        c.close()
        flash("صيغة الفترة غير صحيحة.","error")
        return redirect(url_for("workforce.employee_financial_statement",employee_id=employee_id))
    if month_from and month_to and month_from > month_to:
        c.close()
        flash("بداية الفترة يجب ألا تتجاوز نهايتها.","error")
        return redirect(url_for("workforce.employee_financial_statement",employee_id=employee_id))
    where = ["l.employee_id=?","r.status IN ('posted','reversed')"]
    params = [employee_id]
    if month_from:
        where.append("r.period>=?")
        params.append(month_from)
    if month_to:
        where.append("r.period<=?")
        params.append(month_to)
    lines = c.execute("""SELECT l.*,r.period,r.status run_status,r.journal_entry_id,r.id run_id
        FROM payroll_lines l JOIN payroll_runs r ON r.id=l.run_id
        WHERE """+" AND ".join(where)+" ORDER BY r.period,l.id",params).fetchall()
    details = {}
    paid_by_line = {}
    for line in lines:
        items = c.execute("""SELECT * FROM payroll_line_items WHERE payroll_line_id=?
            ORDER BY section,sort_order,id""",(line["id"],)).fetchall()
        details[line["id"]] = items
        allocations = c.execute("""SELECT a.amount,p.payment_date,p.reference,p.journal_entry_id
            FROM payroll_payment_allocations a JOIN payroll_payments p ON p.id=a.payment_id
            WHERE a.payroll_line_id=? ORDER BY p.payment_date,p.id""",(line["id"],)).fetchall()
        paid_by_line[line["id"]] = allocations
    penalties = c.execute("""SELECT * FROM employee_penalties WHERE employee_id=?
        AND (?='' OR effective_period>=?) AND (?='' OR effective_period<=?)
        ORDER BY effective_period,penalty_date,id""",
        (employee_id,month_from,month_from,month_to,month_to)).fetchall()
    summary = {
        "gross":M(sum((D(x["gross_pay"]) for x in lines),Decimal("0"))),
        "net":M(sum((D(x["net_pay"]) for x in lines),Decimal("0"))),
        "paid":M(sum((D(a["amount"]) for line in lines for a in paid_by_line[line["id"]]),Decimal("0"))),
        "employer_insurance":M(sum((D(x["employer_insurance"]) for x in lines),Decimal("0"))),
    }
    summary["remaining"] = M(max(Decimal("0"),summary["net"]-summary["paid"]))
    unallocated = c.execute("""SELECT p.period,pp.payment_date,pp.amount,pp.reference
        FROM payroll_payments pp JOIN payroll_runs p ON p.id=pp.run_id
        WHERE p.status IN ('posted','reversed')
        AND NOT EXISTS(SELECT 1 FROM payroll_payment_allocations a WHERE a.payment_id=pp.id)
        AND EXISTS(SELECT 1 FROM payroll_lines l WHERE l.run_id=p.id AND l.employee_id=?)
        AND (?='' OR p.period>=?) AND (?='' OR p.period<=?)
        ORDER BY p.period,pp.payment_date""",
        (employee_id,month_from,month_from,month_to,month_to)).fetchall()
    c.close()
    return render_template("employee_statement.html",employee=employee,lines=lines,details=details,
        paid_by_line=paid_by_line,penalties=penalties,summary=summary,
        month_from=month_from,month_to=month_to,unallocated=unallocated)

@workforce_bp.route("/hr/payroll/<int:run_id>/payment/<int:payment_id>/allocate", methods=["GET","POST"])
@_role("admin","accountant")
def allocate_legacy_payroll_payment(run_id, payment_id):
    c = _db()
    payment = c.execute("SELECT * FROM payroll_payments WHERE id=? AND run_id=?",
                        (payment_id,run_id)).fetchone()
    run = c.execute("SELECT * FROM payroll_runs WHERE id=?",(run_id,)).fetchone()
    if not payment or not run:
        c.close()
        flash("دفعة المرتبات غير موجودة.","error")
        return redirect(url_for("workforce.payroll_run",run_id=run_id))
    existing = c.execute("""SELECT a.*,e.employee_code,e.full_name
        FROM payroll_payment_allocations a
        JOIN payroll_lines l ON l.id=a.payroll_line_id
        JOIN employees e ON e.id=l.employee_id
        WHERE a.payment_id=? ORDER BY e.employee_code""",(payment_id,)).fetchall()
    if request.method == "POST":
        try:
            if existing:
                raise ValueError("هذه الدفعة موزعة بالفعل؛ لا يمكن استبدال توزيعها التاريخي من هذه الشاشة.")
            c.execute("BEGIN IMMEDIATE")
            lines = c.execute("""SELECT id,employee_id,net_pay FROM payroll_lines
                WHERE run_id=? ORDER BY id""",(run_id,)).fetchall()
            total = Decimal("0")
            inserts = []
            for line in lines:
                amount = M(request.form.get("allocation_"+str(line["id"]),"0"))
                already = M(sum((D(x["amount"]) for x in c.execute(
                    "SELECT amount FROM payroll_payment_allocations WHERE payroll_line_id=?",
                    (line["id"],)).fetchall()),Decimal("0")))
                if amount < 0 or amount > M(D(line["net_pay"])-already):
                    raise ValueError("توزيع الدفعة يتجاوز صافي المستحق المتبقي لأحد الموظفين.")
                total += amount
                if amount:
                    inserts.append((payment_id,line["id"],str(amount)))
            if M(total) != M(payment["amount"]):
                raise ValueError("يجب أن يساوي مجموع التوزيعات قيمة الدفعة القديمة بالضبط.")
            for item in inserts:
                c.execute("""INSERT INTO payroll_payment_allocations(payment_id,payroll_line_id,amount)
                    VALUES(?,?,?)""",item)
            _audit(c,"payroll_payment_allocated",str(payment_id)+" amount="+str(M(total)))
            c.commit()
            flash("تم توزيع الدفعة القديمة على الموظفين دون إنشاء قيد جديد أو تعديل القيد الأصلي.","success")
            c.close()
            return redirect(url_for("workforce.payroll_run",run_id=run_id))
        except (ValueError,sqlite3.Error) as exc:
            c.rollback()
            flash(str(exc),"error")
    lines = c.execute("""SELECT l.id,l.net_pay,e.employee_code,e.full_name
        FROM payroll_lines l JOIN employees e ON e.id=l.employee_id
        WHERE l.run_id=? ORDER BY e.employee_code""",(run_id,)).fetchall()
    allocated_by_line = {}
    for line in lines:
        allocated_by_line[line["id"]] = M(sum((D(x["amount"]) for x in c.execute(
            "SELECT amount FROM payroll_payment_allocations WHERE payroll_line_id=?",
            (line["id"],)).fetchall()),Decimal("0")))
    c.close()
    return render_template("payroll_payment_allocation.html",payment=payment,run=run,
        lines=lines,existing=existing,allocated_by_line=allocated_by_line)
