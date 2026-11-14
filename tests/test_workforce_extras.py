import sqlite3
import tempfile
import unittest
from pathlib import Path

from flask import Flask
import workforce
import workforce_extras
import payroll_compliance


class WorkforceExtrasTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "nexus-extras.db"
        self.original_path = workforce.DB_PATH
        self.original_compliance_path = payroll_compliance.DB_PATH
        workforce.DB_PATH = self.db_path
        payroll_compliance.DB_PATH = self.db_path
        conn = sqlite3.connect(self.db_path)
        conn.executescript("""
            CREATE TABLE accounts(id INTEGER PRIMARY KEY,code TEXT,name_ar TEXT,account_type TEXT,
                is_group INTEGER DEFAULT 0,active INTEGER DEFAULT 1);
            CREATE TABLE journal_entries(id INTEGER PRIMARY KEY,entry_date TEXT,reference TEXT,
                description TEXT,status TEXT,created_by INTEGER,created_at TEXT);
            CREATE TABLE journal_lines(id INTEGER PRIMARY KEY,entry_id INTEGER,account_id INTEGER,
                debit TEXT DEFAULT '0',credit TEXT DEFAULT '0',memo TEXT);
            CREATE TABLE company_settings(setting_key TEXT PRIMARY KEY,setting_value TEXT NOT NULL);
            INSERT INTO company_settings VALUES('tax_country','EG');
        """)
        conn.commit()
        conn.close()
        workforce.initialize_workforce_schema()
        payroll_compliance.initialize_payroll_compliance_schema()
        workforce_extras.initialize_workforce_extras_schema()
        conn = sqlite3.connect(self.db_path)
        conn.execute("""INSERT INTO employees(employee_code,full_name,hire_date,basic_salary,standard_hours)
            VALUES('E001','Employee One','2026-01-01','10000','200')""")
        conn.commit()
        conn.close()
        template_dir = Path(self.temp.name) / "templates"
        template_dir.mkdir()
        for name, body in {
            "employee_penalties.html": "{{ rows|length }}",
            "employee_statement.html": "{{ employee.full_name }}|{{ summary.net }}",
            "payroll.html": "{{ get_flashed_messages()|join('|') }}",
            "payroll_run.html": "run",
            "friday_allowance.html": "friday",
        }.items():
            (template_dir / name).write_text(body, encoding="utf-8")
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

    def test_penalty_requires_approval_and_is_snapshotted_in_payroll(self):
        response = self.client.post("/hr/penalties", data={
            "employee_id": "1", "penalty_date": "2026-11-10",
            "effective_period": "2026-11", "amount": "250",
            "duration_days": "0", "reason": "جزاء تأخير",
        })
        self.assertEqual(response.status_code, 302)
        conn = sqlite3.connect(self.db_path)
        self.assertEqual(conn.execute("SELECT status FROM employee_penalties").fetchone()[0], "requested")
        conn.close()

        self.client.post("/hr/penalties/1/decision", data={"decision": "approved"})
        response = self.client.post("/hr/payroll", data={
            "period": "2026-11", "basic_1": "10000", "allowances_1": "0",
            "overtime_hours_1": "0", "overtime_amount_1": "",
            "overtime_rate_percent_1": "150", "overtime_override_reason_1": "",
            "absent_hours_1": "0", "absence_deduction_1": "0",
            "advance_1": "0", "other_deductions_1": "0",
            "tax_1": "0", "employee_ins_1": "0", "employer_ins_1": "0",
            "incentives_1": "0",
        })
        self.assertEqual(response.status_code, 302, response.get_data(as_text=True))
        conn = sqlite3.connect(self.db_path)
        line = conn.execute("SELECT other_deductions,penalty_deduction FROM payroll_lines").fetchone()
        item = conn.execute("""SELECT item_name,amount FROM payroll_line_items
            WHERE source_type='employee_penalty'""").fetchone()
        status = conn.execute("SELECT status FROM employee_penalties").fetchone()[0]
        conn.close()
        self.assertEqual(line, ("0.00", "250.00"))
        self.assertEqual(item, ("جزاء: جزاء تأخير", "250.00"))
        self.assertEqual(status, "approved")

    def test_full_month_unpaid_leave_is_not_added_to_payroll(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE employees SET work_status='unpaid_leave' WHERE id=1")
        conn.commit()
        conn.close()
        response = self.client.post("/hr/payroll", data={"period":"2026-11"}, follow_redirects=True)
        self.assertIn("لا يوجد موظفون مستحقون", response.get_data(as_text=True))
        conn = sqlite3.connect(self.db_path)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM payroll_runs").fetchone()[0],0)
        conn.close()

    def test_employee_statement_keeps_historical_and_unallocated_payments_distinct(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("""INSERT INTO payroll_runs(id,period,status,journal_entry_id)
            VALUES(1,'2026-10','posted',1)""")
        conn.execute("""INSERT INTO payroll_lines(run_id,employee_id,basic_salary,allowances,overtime_hours,
            overtime_amount,absent_hours,absence_deduction,other_deductions,taxable_income_tax,
            employee_insurance,employer_insurance,gross_pay,net_pay)
            VALUES(1,1,'10000','0','0','0','0','0','0','0','0','0','10000','10000')""")
        conn.commit()
        conn.close()
        response = self.client.get("/hr/employee/1/statement")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Employee One", response.get_data(as_text=True))


    def test_employee_edit_persists_basic_variable_and_insured_wages_separately(self):
        response=self.client.post("/hr/1/edit",data={
            "employee_code":"E001","full_name":"Employee One","job_title":"محاسب","department":"الحسابات",
            "hire_date":"2026-01-01","basic_salary":"12000","standard_hours":"208",
            "variable_salary":"1800","legal_category":"local","insurance_number":"INS-1",
            "insured_wage":"9500","insurance_enabled":"1","address":"Tanta",
            "national_id":"12345678901234","work_status":"permanent","raise_type":"fixed",
            "raise_value":"0","raise_mode":"basic","raise_effective_date":"",
            "overtime_rate_percent":"150","branch_id":"","cost_center_id":"","project_id":"",
            "termination_date":""
        })
        self.assertEqual(response.status_code,302)
        conn=sqlite3.connect(self.db_path)
        wages=conn.execute("SELECT basic_salary,variable_salary,insured_wage FROM employees WHERE id=1").fetchone()
        conn.close()
        self.assertEqual(wages,("12000.00","1800.00","9500.00"))


    def test_independent_employee_grant_snapshots_calculation_and_posts_two_journals(self):
        conn=sqlite3.connect(self.db_path)
        conn.executemany("""INSERT INTO accounts(id,code,name_ar,account_type,is_group,active)
            VALUES(?,?,?,?,0,1)""",[
            (1,"1000","الصندوق","asset"),
            (2,"5000","مصروف المنح","expense"),
            (3,"2100","منح مستحقة","liability"),
        ])
        conn.commit(); conn.close()
        self.client.post("/hr/grants/types",data={"name":"منحة رمضان"})
        response=self.client.post("/hr/grants/create",data={
            "grant_type_id":"1","grant_date":"2026-11-14","calculation_method":"per_day",
            "calculation_value":"0","days_count":"5","expense_account_id":"2",
            "payable_account_id":"3","employee_ids":["1"],"notes":"اختبار"
        })
        self.assertEqual(response.status_code,302)
        conn=sqlite3.connect(self.db_path)
        grant=conn.execute("SELECT total_amount,status FROM employee_grants WHERE id=1").fetchone()
        line=conn.execute("SELECT basic_salary_snapshot,amount,paid_amount FROM employee_grant_lines WHERE grant_id=1").fetchone()
        conn.execute("UPDATE employees SET basic_salary='12000' WHERE id=1")
        conn.commit(); conn.close()
        self.assertEqual(grant,("1666.67","draft"))
        self.assertEqual(line,("10000.00","1666.67","0"))
        approved=self.client.post("/hr/grants/1/approve",data={})
        self.assertEqual(approved.status_code,302)
        conn=sqlite3.connect(self.db_path)
        journal=conn.execute("SELECT status FROM journal_entries WHERE reference='EMP-GRANT-1'").fetchone()
        journal_lines=conn.execute("SELECT account_id,debit,credit FROM journal_lines WHERE entry_id=(SELECT journal_entry_id FROM employee_grants WHERE id=1) ORDER BY id").fetchall()
        conn.close()
        self.assertEqual(journal,("posted",))
        self.assertEqual(journal_lines,[(2,"1666.67","0.00"),(3,"0.00","1666.67")])
        paid=self.client.post("/hr/grants/1/pay",data={
            "amount":"500","payment_date":"2026-11-15","cash_account_id":"1"
        })
        self.assertEqual(paid.status_code,302)
        conn=sqlite3.connect(self.db_path)
        grant_status=conn.execute("SELECT status FROM employee_grants WHERE id=1").fetchone()[0]
        paid_line=conn.execute("SELECT paid_amount FROM employee_grant_lines WHERE grant_id=1").fetchone()[0]
        payment_lines=conn.execute("SELECT account_id,debit,credit FROM journal_lines WHERE entry_id=(SELECT journal_entry_id FROM employee_grant_payments WHERE grant_id=1) ORDER BY id").fetchall()
        conn.close()
        self.assertEqual(grant_status,"partially_paid")
        self.assertEqual(paid_line,"500.00")
        self.assertEqual(payment_lines,[(3,"500.00","0.00"),(1,"0.00","500.00")])


    def test_monthly_attendance_matrix_saves_multiple_days_and_leave_statuses(self):
        response=self.client.post("/hr/attendance",data={
            "save_month":"1","month":"2026-11",
            "status_1_1":"present","check_in_1_1":"08:00","check_out_1_1":"16:00",
            "status_1_2":"absent","absence_1_2":"8",
            "status_1_3":"unpaid_leave",
        })
        self.assertEqual(response.status_code,302)
        conn=sqlite3.connect(self.db_path)
        rows=conn.execute("SELECT attendance_date,status,check_in,check_out,absence_hours FROM attendance_records ORDER BY attendance_date").fetchall()
        conn.close()
        self.assertEqual(rows,[
            ("2026-11-01","present","08:00","16:00","0.00"),
            ("2026-11-02","absent","","","8.00"),
            ("2026-11-03","unpaid_leave","","","0.00"),
        ])


    def test_friday_allowance_uses_basic_salary_only_and_posts_accrual_once(self):
        conn=sqlite3.connect(self.db_path)
        conn.executemany("""INSERT INTO accounts(id,code,name_ar,account_type,is_group,active)
            VALUES(?,?,?,?,0,1)""",[
            (1,"1000","الصندوق","asset"),
            (2,"5200","مصروف أجور إضافية","expense"),
            (3,"2200","أجور إضافية مستحقة","liability"),
        ])
        conn.execute("UPDATE employees SET friday_work_eligible=1,variable_salary='5000',insured_wage='8000' WHERE id=1")
        conn.executemany("""INSERT INTO attendance_records(employee_id,attendance_date,status,check_in,check_out)
            VALUES(1,? ,?,'08:00','16:00')""",[
            ("2026-11-06","present"),("2026-11-13","present"),
            ("2026-11-20","absent"),("2026-11-07","present"),
        ])
        conn.commit(); conn.close()
        prepared=self.client.post("/hr/friday-allowance",data={
            "period":"2026-11","expense_account_id":"2","payable_account_id":"3"
        })
        self.assertEqual(prepared.status_code,302)
        conn=sqlite3.connect(self.db_path)
        batch=conn.execute("SELECT id,status,total_amount FROM friday_allowance_batches WHERE period='2026-11'").fetchone()
        line=conn.execute("""SELECT basic_salary_snapshot,attendance_days,amount
            FROM friday_allowance_lines WHERE batch_id=?""",(batch[0],)).fetchone()
        conn.close()
        self.assertEqual(batch, (1,"draft","1333.33"))
        self.assertEqual(line,("10000.00",2,"1333.33"))
        posted=self.client.post("/hr/friday-allowance/1/post",data={})
        self.assertEqual(posted.status_code,302)
        conn=sqlite3.connect(self.db_path)
        status,journal_id=conn.execute("SELECT status,journal_entry_id FROM friday_allowance_batches WHERE id=1").fetchone()
        journal=conn.execute("SELECT reference,status,entry_date FROM journal_entries WHERE id=?",(journal_id,)).fetchone()
        journal_lines=conn.execute("""SELECT account_id,debit,credit FROM journal_lines
            WHERE entry_id=? ORDER BY id""",(journal_id,)).fetchall()
        conn.close()
        self.assertEqual(status,"posted")
        self.assertEqual(journal,("FRIDAY-ALLOWANCE-2026-11","posted","2026-11-30"))
        self.assertEqual(journal_lines,[(2,"1333.33","0.00"),(3,"0.00","1333.33")])
        duplicate=self.client.post("/hr/friday-allowance/1/post",data={})
        self.assertEqual(duplicate.status_code,302)
        conn=sqlite3.connect(self.db_path)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM journal_entries WHERE reference='FRIDAY-ALLOWANCE-2026-11'").fetchone()[0],1)
        conn.close()


    def test_employee_friday_allowance_flag_is_saved_from_management_choice(self):
        response=self.client.post("/hr/1/edit",data={
            "employee_code":"E001","full_name":"Employee One","job_title":"محاسب","department":"الحسابات",
            "hire_date":"2026-01-01","basic_salary":"10000","standard_hours":"208",
            "variable_salary":"0","legal_category":"local","insurance_number":"INS-1",
            "insured_wage":"8000","insurance_enabled":"1","address":"Tanta",
            "national_id":"12345678901234","work_status":"permanent","raise_type":"fixed",
            "raise_value":"0","raise_mode":"basic","raise_effective_date":"",
            "overtime_rate_percent":"150","branch_id":"","cost_center_id":"","project_id":"",
            "termination_date":"","friday_work_eligible":"1"
        })
        self.assertEqual(response.status_code,302)
        conn=sqlite3.connect(self.db_path)
        self.assertEqual(conn.execute("SELECT friday_work_eligible FROM employees WHERE id=1").fetchone()[0],1)
        conn.close()


if __name__ == "__main__":
    unittest.main()
