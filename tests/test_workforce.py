import sqlite3
import tempfile
import unittest
from pathlib import Path

from flask import Flask
import payroll_compliance
import workforce


class WorkforceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "nexus.db"
        self.original_path = workforce.DB_PATH
        self.original_compliance_path = payroll_compliance.DB_PATH
        workforce.DB_PATH = self.db_path
        payroll_compliance.DB_PATH = self.db_path
        conn = sqlite3.connect(self.db_path)
        conn.executescript("""
          CREATE TABLE accounts(id INTEGER PRIMARY KEY, code TEXT, name_ar TEXT, account_type TEXT,
            is_group INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1);
          CREATE TABLE journal_entries(id INTEGER PRIMARY KEY, entry_date TEXT, reference TEXT,
            description TEXT, status TEXT, created_by INTEGER, created_at TEXT);
          CREATE TABLE journal_lines(id INTEGER PRIMARY KEY, entry_id INTEGER, account_id INTEGER,
            debit TEXT DEFAULT '0', credit TEXT DEFAULT '0', memo TEXT);
          INSERT INTO accounts VALUES(1,'1000','الصندوق','asset',0,1);
          INSERT INTO accounts VALUES(2,'1300','عهد الموظفين','asset',0,1);
          INSERT INTO accounts VALUES(3,'6000','مصروفات','expense',0,1);
          INSERT INTO accounts VALUES(4,'6100','مصروف أجور','expense',0,1);
          INSERT INTO accounts VALUES(5,'2100','رواتب مستحقة','liability',0,1);
          INSERT INTO accounts VALUES(6,'2110','ضريبة مرتبات','liability',0,1);
          INSERT INTO accounts VALUES(7,'2120','تأمينات مستحقة','liability',0,1);
          CREATE TABLE company_settings(setting_key TEXT PRIMARY KEY, setting_value TEXT NOT NULL);
          INSERT INTO company_settings VALUES('tax_country','EG');
        """)
        conn.commit()
        conn.close()
        workforce.initialize_workforce_schema()
        payroll_compliance.initialize_payroll_compliance_schema()
        payroll_compliance.ensure_default_payroll_rules(payroll_compliance._db())
        template_dir = Path(self.temp.name) / "templates"
        template_dir.mkdir()
        (template_dir / "custody.html").write_text("custody list", encoding="utf-8")
        (template_dir / "payroll.html").write_text(
            "{{ get_flashed_messages()|join('|') }}", encoding="utf-8"
        )
        (template_dir / "payroll_run.html").write_text(
            "{{ get_flashed_messages()|join('|') }}", encoding="utf-8"
        )
        self.app = Flask(__name__, template_folder=str(template_dir))
        self.app.secret_key = "test"
        self.app.register_blueprint(workforce.workforce_bp)
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session["user_id"] = 1
            session["role"] = "accountant"

    def tearDown(self):
        workforce.DB_PATH = self.original_path
        payroll_compliance.DB_PATH = self.original_compliance_path
        self.temp.cleanup()

    def add_employee(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("""INSERT INTO employees(employee_code,full_name,hire_date,basic_salary,standard_hours)
                        VALUES('E001','Employee One','2026-01-01','10000','200')""")
        conn.execute("""INSERT INTO employee_pay_items(employee_id,item_name,item_type,amount)
                        VALUES(1,'بدل انتقال','allowance','1000')""")
        conn.commit()
        conn.close()

    def test_payroll_calculation_and_posting(self):
        self.add_employee()
        response = self.client.post("/hr/payroll", data={
            "period": "2026-11", "basic_1": "12000", "allowances_1": "1500",
            "overtime_hours_1": "10", "overtime_amount_1": "750",
            "overtime_override_reason_1": "اعتماد يدوي بعد مراجعة المشرف",
            "absent_hours_1": "2", "absence_deduction_1": "100",
            "other_deductions_1": "200", "tax_1": "500",
            "employee_ins_1": "300", "employer_ins_1": "400"
        })
        self.assertEqual(response.status_code, 302, response.get_data(as_text=True))
        conn = sqlite3.connect(self.db_path)
        line = conn.execute("SELECT gross_pay,net_pay FROM payroll_lines").fetchone()
        conn.close()
        self.assertEqual(line, ("15250.00", "14150.00"))
        conn = sqlite3.connect(self.db_path)
        manual_values = conn.execute(
            "SELECT basic_salary,allowances,taxable_income_tax,employee_insurance,employer_insurance,rule_set_id "
            "FROM payroll_lines WHERE run_id=1"
        ).fetchone()
        conn.close()
        # Even though the test setup seeded Egypt's automatic rule set, manual
        # amounts must remain unchanged and no statutory rule is snapshotted.
        self.assertEqual(manual_values, ("12000.00", "2500.00", "500.00", "300.00", "400.00", None))
        response = self.client.post("/hr/payroll/1/post", data={
            "salary_expense_account": "4", "employer_ins_expense_account": "3",
            "salaries_payable_account": "5", "tax_payable_account": "6",
            "insurance_payable_account": "7", "advances_account": "2",
            "other_deductions_account": "2"
        })
        self.assertEqual(response.status_code, 302)
        conn = sqlite3.connect(self.db_path)
        status = conn.execute("SELECT status FROM payroll_runs WHERE id=1").fetchone()[0]
        journal_count = conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0]
        conn.close()
        self.assertEqual(status, "posted")
        self.assertEqual(journal_count, 1)
        conn = sqlite3.connect(self.db_path)
        posted = conn.execute(
            "SELECT a.code,jl.debit,jl.credit,jl.memo FROM journal_lines jl "
            "JOIN accounts a ON a.id=jl.account_id ORDER BY jl.id"
        ).fetchall()
        conn.close()
        self.assertEqual(sum(float(x[1] or 0) for x in posted), 15650.0)
        self.assertEqual(sum(float(x[2] or 0) for x in posted), 15650.0)
        self.assertTrue(any(x[0] == "1300" and x[2] == "200.00" for x in posted))

    def test_payroll_payment_supports_partial_and_full_settlement(self):
        self.add_employee()
        self.client.post("/hr/payroll", data={
            "period": "2026-11", "basic_1": "12000", "allowances_1": "1500",
            "overtime_hours_1": "10", "overtime_amount_1": "750",
            "overtime_override_reason_1": "اعتماد يدوي بعد مراجعة المشرف",
            "absent_hours_1": "2", "absence_deduction_1": "100",
            "other_deductions_1": "200", "tax_1": "500",
            "employee_ins_1": "300", "employer_ins_1": "400"
        })
        self.client.post("/hr/payroll/1/post", data={
            "salary_expense_account": "4", "employer_ins_expense_account": "3",
            "salaries_payable_account": "5", "tax_payable_account": "6",
            "insurance_payable_account": "7", "advances_account": "2",
            "other_deductions_account": "2"
        })
        response = self.client.post("/hr/payroll/1/payment", data={
            "payment_date": "2026-11-30", "amount": "5000", "cash_account_id": "1",
            "notes": "دفعة أولى"
        })
        self.assertEqual(response.status_code, 302)
        response = self.client.post("/hr/payroll/1/payment", data={
            "payment_date": "2026-12-01", "amount": "9150", "cash_account_id": "1"
        })
        self.assertEqual(response.status_code, 302)
        response = self.client.post("/hr/payroll/1/payment", data={
            "payment_date": "2026-12-02", "amount": "1", "cash_account_id": "1"
        }, follow_redirects=True)
        self.assertIn("يتجاوز صافي المرتبات المتبقي", response.get_data(as_text=True))
        conn = sqlite3.connect(self.db_path)
        payments = conn.execute("SELECT amount FROM payroll_payments WHERE run_id=1 ORDER BY id").fetchall()
        debit, credit = conn.execute("""SELECT SUM(debit),SUM(credit) FROM journal_lines
            WHERE entry_id IN (SELECT journal_entry_id FROM payroll_payments)""").fetchone()
        conn.close()
        self.assertEqual(payments, [("5000.00",), ("9150.00",)])
        self.assertAlmostEqual(debit, 14150.0, places=2)
        self.assertAlmostEqual(credit, 14150.0, places=2)

    def test_employee_loan_request_approval_disbursement_and_payroll_deduction(self):
        self.add_employee()
        conn=sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO accounts VALUES(8,'1171','سلف العاملين','asset',0,1)")
        conn.commit(); conn.close()
        self.client.post("/hr/loans",data={"employee_id":"1","requested_amount":"1000",
            "installment_count":"3","first_due_period":"2026-11","notes":"طلب سلفة"})
        conn=sqlite3.connect(self.db_path)
        self.assertEqual(conn.execute("SELECT status FROM employee_loans").fetchone()[0],"requested")
        conn.close()
        self.client.post("/hr/loans/1/approve",data={"approved_amount":"1000",
            "installment_count":"3","first_due_period":"2026-11"})
        self.client.post("/hr/loans/1/disburse",data={"cash_account_id":"1","disbursement_date":"2026-11-01"})
        conn=sqlite3.connect(self.db_path)
        installments=conn.execute("SELECT due_period,due_amount FROM employee_loan_installments ORDER BY due_period").fetchall()
        conn.close()
        self.assertEqual(installments,[("2026-11","333.34"),("2026-12","333.33"),("2027-01","333.33")])
        self.client.post("/hr/payroll",data={"period":"2026-11","basic_1":"12000","allowances_1":"1500",
            "overtime_hours_1":"0","overtime_amount_1":"","absent_hours_1":"0","absence_deduction_1":"0",
            "advance_1":"0","other_deductions_1":"0","tax_1":"500","employee_ins_1":"300","employer_ins_1":"400"})
        conn=sqlite3.connect(self.db_path)
        line=conn.execute("SELECT advance_deduction,loan_deduction,net_pay FROM payroll_lines").fetchone()
        conn.close()
        self.assertEqual(line,("333.34","333.34","13366.66"))
        response=self.client.post("/hr/payroll/1/post",data={"salary_expense_account":"4",
            "employer_ins_expense_account":"3","salaries_payable_account":"5","tax_payable_account":"6",
            "insurance_payable_account":"7","advances_account":"8","other_deductions_account":"2"})
        self.assertEqual(response.status_code,302)
        conn=sqlite3.connect(self.db_path)
        paid=conn.execute("SELECT paid_amount,status FROM employee_loan_installments WHERE due_period='2026-11'").fetchone()
        balance=conn.execute("SELECT remaining_balance FROM employee_loans WHERE id=1").fetchone()[0]
        allocation=conn.execute("SELECT amount FROM payroll_loan_allocations").fetchone()[0]
        conn.close()
        self.assertEqual(paid,("333.34","paid"))
        self.assertEqual(balance,"666.66")
        self.assertEqual(allocation,"333.34")

    def test_attendance_leave_approval_and_payroll_integration(self):
        self.add_employee()
        conn=sqlite3.connect(self.db_path)
        conn.execute("""INSERT INTO employee_leave_balances(employee_id,leave_type_id,balance_days)
            VALUES(1,1,'15.00')""")
        conn.commit(); conn.close()
        # Annual leave approval consumes the employee's available balance.
        self.client.post("/hr/leaves",data={"employee_id":"1","leave_type_id":"1",
            "start_date":"2026-11-02","end_date":"2026-11-03","days":"2","reason":"راحة"})
        response=self.client.post("/hr/leaves/1/decision",data={"decision":"approved"})
        self.assertEqual(response.status_code,302)
        conn=sqlite3.connect(self.db_path)
        self.assertEqual(conn.execute("SELECT balance_days FROM employee_leave_balances").fetchone()[0],"13.00")
        conn.close()
        # Approved unpaid leave is included in the payroll deduction.
        self.client.post("/hr/leaves",data={"employee_id":"1","leave_type_id":"2",
            "start_date":"2026-11-10","end_date":"2026-11-11","days":"2","reason":"بدون أجر"})
        self.client.post("/hr/leaves/2/decision",data={"decision":"approved"})
        self.client.post("/hr/attendance",data={"attendance_date":"2026-11-05","employee_id":"1",
            "status":"absent","absence_hours":"4","approved_overtime_hours":"0"})
        self.client.post("/hr/attendance",data={"attendance_date":"2026-11-06","employee_id":"1",
            "status":"present","absence_hours":"0","approved_overtime_hours":"2"})
        self.client.post("/hr/payroll",data={"period":"2026-11","use_attendance_data":"1",
            "basic_1":"10000","allowances_1":"0","overtime_hours_1":"0","overtime_amount_1":"",
            "overtime_rate_percent_1":"150","absent_hours_1":"0","absence_deduction_1":"0",
            "advance_1":"0","other_deductions_1":"0","tax_1":"0","employee_ins_1":"0",
            "employer_ins_1":"0"})
        conn=sqlite3.connect(self.db_path)
        line=conn.execute("SELECT overtime_hours,overtime_amount,absent_hours,absence_deduction,net_pay FROM payroll_lines").fetchone()
        conn.close()
        self.assertEqual(line,("2.00","150.00","4.00","866.67","10283.33"))

    def test_custody_issue_expense_and_full_settlement(self):
        self.add_employee()
        response = self.client.post("/hr/custody", data={
            "employee_id": "1", "issue_date": "2026-11-10", "amount": "5000",
            "custody_account_id": "2", "cash_account_id": "1", "description": "مشتريات مكتبية"
        })
        self.assertEqual(response.status_code, 302)
        response = self.client.post("/hr/custody/1/expense", data={
            "expense_date": "2026-11-10", "expense_account_id": "3",
            "amount": "2000", "description": "مستلزمات", "receipt_reference": "R-1"
        })
        self.assertEqual(response.status_code, 302)
        response = self.client.post("/hr/custody/1/settle", data={
            "settlement_date": "2026-11-10", "returned_amount": "3000"
        })
        self.assertEqual(response.status_code, 302)
        conn = sqlite3.connect(self.db_path)
        state = conn.execute("SELECT status,remaining_amount FROM employee_custodies WHERE id=1").fetchone()
        debit, credit = conn.execute("SELECT SUM(debit),SUM(credit) FROM journal_lines").fetchone()
        conn.close()
        self.assertEqual(state, ("settled", "0"))
        self.assertAlmostEqual(debit, credit, places=2)

    def test_custody_cannot_settle_with_unreconciled_difference(self):
        self.add_employee()
        self.client.post("/hr/custody", data={
            "employee_id": "1", "issue_date": "2026-11-10", "amount": "5000",
            "custody_account_id": "2", "cash_account_id": "1", "description": "عهدة"
        })
        self.client.post("/hr/custody/1/expense", data={
            "expense_date": "2026-11-10", "expense_account_id": "3",
            "amount": "2000", "description": "مستلزمات"
        })
        self.client.post("/hr/custody/1/settle", data={
            "settlement_date": "2026-11-10", "returned_amount": "2500"
        })
        conn = sqlite3.connect(self.db_path)
        state = conn.execute("SELECT status FROM employee_custodies WHERE id=1").fetchone()[0]
        conn.close()
        self.assertEqual(state, "open")


    def test_payroll_rejects_empty_or_zero_basic_salary(self):
        self.add_employee()
        for value in ("", "0", "0.00"):
            response = self.client.post("/hr/payroll", data={
                "period": "2026-11", "basic_1": value
            }, follow_redirects=True)
            self.assertIn("الراتب الأساسي", response.get_data(as_text=True))
        conn = sqlite3.connect(self.db_path)
        count = conn.execute("SELECT COUNT(*) FROM payroll_runs").fetchone()[0]
        conn.close()
        self.assertEqual(count, 0)

    def test_multi_employee_payroll_totals_and_posted_journal(self):
        self.add_employee()
        conn = sqlite3.connect(self.db_path)
        conn.execute("""INSERT INTO employees(employee_code,full_name,hire_date,basic_salary,standard_hours)
                        VALUES('E002','Employee Two','2026-01-01','8000','200')""")
        conn.commit()
        conn.close()
        response = self.client.post("/hr/payroll", data={
            "period": "2026-11",
            "basic_1": "12000", "allowances_1": "1500", "overtime_amount_1": "750",
            "overtime_override_reason_1": "اعتماد يدوي بعد مراجعة المشرف",
            "absence_deduction_1": "100", "other_deductions_1": "200",
            "tax_1": "500", "employee_ins_1": "300", "employer_ins_1": "400",
            "basic_2": "8000", "allowances_2": "500", "overtime_amount_2": "0",
            "absence_deduction_2": "0", "other_deductions_2": "100",
            "tax_2": "200", "employee_ins_2": "150", "employer_ins_2": "250"
        })
        self.assertEqual(response.status_code, 302, response.get_data(as_text=True))
        conn = sqlite3.connect(self.db_path)
        totals = conn.execute("SELECT SUM(CAST(gross_pay AS NUMERIC)),SUM(CAST(net_pay AS NUMERIC)) FROM payroll_lines").fetchone()
        conn.close()
        self.assertEqual(totals, (23750, 22200))
        response = self.client.post("/hr/payroll/1/post", data={
            "salary_expense_account": "4", "employer_ins_expense_account": "3",
            "salaries_payable_account": "5", "tax_payable_account": "6",
            "insurance_payable_account": "7", "advances_account": "2",
            "other_deductions_account": "2"
        })
        self.assertEqual(response.status_code, 302)
        conn = sqlite3.connect(self.db_path)
        debit, credit = conn.execute("SELECT SUM(CAST(debit AS NUMERIC)),SUM(CAST(credit AS NUMERIC)) FROM journal_lines").fetchone()
        totals_by_account = conn.execute(
            "SELECT account_id,SUM(CAST(debit AS NUMERIC)),SUM(CAST(credit AS NUMERIC)) FROM journal_lines GROUP BY account_id"
        ).fetchall()
        conn.close()
        self.assertEqual(debit, credit)
        self.assertEqual(debit, 24400)
        self.assertIn((4, 23750, 100), totals_by_account)
        self.assertIn((5, 0, 22200), totals_by_account)

    def test_employee_profile_fields_and_salary_raise_overtime_and_event_bonus(self):
        response = self.client.post("/hr", data={
            "employee_code": "E003", "full_name": "Employee Three", "national_id": "12345678901234",
            "insurance_number": "INS-3", "address": "Tanta", "job_title": "Accountant",
            "department": "Finance", "hire_date": "2026-01-01", "basic_salary": "10000",
            "standard_hours": "200", "legal_category": "local", "insured_wage": "0",
            "insurance_enabled": "1", "work_status": "permanent", "raise_type": "percentage",
            "raise_value": "10", "raise_mode": "basic", "raise_effective_date": "2026-11-01",
            "overtime_rate_percent": "150"
        })
        self.assertEqual(response.status_code, 302)
        conn = sqlite3.connect(self.db_path)
        employee = conn.execute(
            "SELECT address,national_id,insurance_number,work_status,raise_type,raise_value,raise_mode "
            "FROM employees WHERE employee_code='E003'"
        ).fetchone()
        conn.execute("""INSERT INTO payroll_incentive_rules(name,calculation_method,calculation_value)
                        VALUES('حافز مناسبة','percentage_basic','5')""")
        conn.execute("""INSERT INTO employees(employee_code,full_name,hire_date,basic_salary,standard_hours,work_status)
                        VALUES('E005','Employee Five','2026-01-01','8000','200','permanent')""")
        conn.execute("""INSERT INTO payroll_incentive_rules(name,calculation_method,calculation_value)
                        VALUES('حافز أيام','per_day','100')""")
        conn.commit()
        conn.close()
        self.assertEqual(employee, ("Tanta","12345678901234","INS-3","permanent","percentage","10.00","basic"))
        response = self.client.post("/hr/payroll", data={
            "period": "2026-11", "basic_1": "10000", "allowances_1": "500",
            "overtime_hours_1": "10", "overtime_rate_percent_1": "150", "overtime_amount_1": "",
            "incentive_rule_1": "1", "incentive_days_1": "0", "incentives_1": "0",
            "absence_deduction_1": "0", "other_deductions_1": "0", "advance_1": "0",
            "tax_1": "0", "employee_ins_1": "0", "employer_ins_1": "0",
            "basic_2": "8000", "allowances_2": "0", "overtime_hours_2": "0",
            "overtime_rate_percent_2": "150", "overtime_amount_2": "",
            "incentive_rule_2": "2", "incentive_days_2": "3", "incentives_2": "0",
            "absence_deduction_2": "0", "other_deductions_2": "0", "advance_2": "0",
            "tax_2": "0", "employee_ins_2": "0", "employer_ins_2": "0"
        })
        self.assertEqual(response.status_code, 302)
        conn = sqlite3.connect(self.db_path)
        line = conn.execute(
            "SELECT basic_salary,salary_raise,overtime_amount,incentives,gross_pay,incentive_name "
            "FROM payroll_lines WHERE run_id=1 AND employee_id=1"
        ).fetchone()
        per_day = conn.execute(
            "SELECT incentives,gross_pay,incentive_days FROM payroll_lines WHERE run_id=1 AND employee_id=2"
        ).fetchone()
        conn.close()
        self.assertEqual(line, ("11000.00","1000.00","825.00","550.00","12875.00","حافز مناسبة"))
        self.assertEqual(per_day, ("300.00","8300.00","3"))

    def test_payroll_raise_fixed_items_and_overtime_override_are_snapshotted(self):
        self.add_employee()
        conn = sqlite3.connect(self.db_path)
        conn.execute("""UPDATE employees SET raise_type='percentage',raise_value='10',
            raise_mode='basic',raise_effective_date='2026-11-01' WHERE id=1""")
        conn.execute("""INSERT INTO employee_pay_items(employee_id,item_name,item_type,amount)
            VALUES(1,'خصم ثابت','deduction','250')""")
        conn.commit()
        conn.close()
        response = self.client.post("/hr/payroll", data={
            "period": "2026-11", "basic_1": "11000", "allowances_1": "500",
            "overtime_hours_1": "10", "overtime_rate_percent_1": "150",
            "overtime_amount_1": "700", "overtime_override_reason_1": "موافقة مدير التشغيل",
            "other_deductions_1": "50", "tax_1": "0", "employee_ins_1": "0",
            "employer_ins_1": "0", "absence_deduction_1": "0"
        })
        self.assertEqual(response.status_code, 302)
        conn = sqlite3.connect(self.db_path)
        row = conn.execute("""SELECT basic_salary,salary_raise,allowances,overtime_calculated_amount,
            overtime_amount,overtime_manual_override,overtime_override_reason,other_deductions,gross_pay,net_pay
            FROM payroll_lines WHERE run_id=1""").fetchone()
        conn.close()
        self.assertEqual(row, ("11000.00","1000.00","1500.00","825.00","700.00",1,
                               "موافقة مدير التشغيل","300.00","13200.00","12900.00"))

    def test_manual_overtime_difference_requires_reason(self):
        self.add_employee()
        response = self.client.post("/hr/payroll", data={
            "period": "2026-11", "basic_1": "10000", "allowances_1": "0",
            "overtime_hours_1": "10", "overtime_rate_percent_1": "150",
            "overtime_amount_1": "500", "other_deductions_1": "0",
            "tax_1": "0", "employee_ins_1": "0", "employer_ins_1": "0",
            "absence_deduction_1": "0"
        }, follow_redirects=True)
        self.assertIn("اكتب سبب اختلاف مبلغ الإضافي اليدوي", response.get_data(as_text=True))
        conn = sqlite3.connect(self.db_path)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM payroll_runs").fetchone()[0], 0)
        conn.close()

    def test_workforce_backfill_migration_runs_once(self):
        conn = sqlite3.connect(self.db_path)
        first = conn.execute("SELECT applied_at FROM workforce_migrations WHERE migration_key='backfill_payroll_totals_v1'").fetchone()[0]
        conn.close()
        workforce.initialize_workforce_schema()
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute("SELECT applied_at FROM workforce_migrations WHERE migration_key='backfill_payroll_totals_v1'").fetchall()
        conn.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], first)

    def test_payroll_excludes_unpaid_leave_resigned_and_retired_employees(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("""INSERT INTO employees(employee_code,full_name,hire_date,basic_salary,standard_hours,work_status)
                        VALUES('E004','On Leave','2026-01-01','5000','200','unpaid_leave')""")
        conn.commit()
        conn.close()
        response = self.client.post("/hr/payroll", data={"period":"2026-11"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("لا يوجد موظفون مستحقون", response.get_data(as_text=True))
        conn = sqlite3.connect(self.db_path)
        count = conn.execute("SELECT COUNT(*) FROM payroll_lines").fetchone()[0]
        run_count = conn.execute("SELECT COUNT(*) FROM payroll_runs").fetchone()[0]
        conn.close()
        self.assertEqual(count, 0)
        self.assertEqual(run_count, 0)

    def test_optional_automatic_statutory_calculation_snapshots_rules(self):
        self.add_employee()
        response = self.client.post("/hr/payroll", data={
            "period": "2026-11", "auto_calculate_statutory": "1",
            "basic_1": "12000", "allowances_1": "1000",
            "overtime_hours_1": "0", "overtime_amount_1": "0",
            "absent_hours_1": "0", "absence_deduction_1": "0",
            "other_deductions_1": "0", "tax_1": "0",
            "employee_ins_1": "0", "employer_ins_1": "0"
        })
        self.assertEqual(response.status_code, 302, response.get_data(as_text=True))
        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT taxable_income_tax,employee_insurance,employer_insurance,rule_set_id,taxable_annual,insured_wage "
            "FROM payroll_lines WHERE run_id=1"
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row[3])
        self.assertGreater(float(row[0]), 0)
        self.assertGreater(float(row[1]), 0)
        self.assertGreater(float(row[2]), 0)
        self.assertGreater(float(row[4]), 0)
        self.assertGreater(float(row[5]), 0)

if __name__ == "__main__":
    unittest.main()
