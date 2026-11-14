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


if __name__ == "__main__":
    unittest.main()
