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
            "period": "2026-11", "overtime_1": "10", "absent_1": "2",
            "tax_1": "500", "employee_ins_1": "300", "employer_ins_1": "400"
        })
        self.assertEqual(response.status_code, 302)
        conn = sqlite3.connect(self.db_path)
        line = conn.execute("SELECT gross_pay,net_pay FROM payroll_lines").fetchone()
        conn.close()
        self.assertEqual(line, ("11650.00", "10850.00"))
        conn = sqlite3.connect(self.db_path)
        manual_values = conn.execute(
            "SELECT taxable_income_tax,employee_insurance,employer_insurance,rule_set_id "
            "FROM payroll_lines WHERE run_id=1"
        ).fetchone()
        conn.close()
        # Even though the test setup seeded Egypt's automatic rule set, manual
        # amounts must remain unchanged and no statutory rule is snapshotted.
        self.assertEqual(manual_values, ("500.00", "300.00", "400.00", None))
        response = self.client.post("/hr/payroll/1/post", data={
            "salary_expense_account": "4", "employer_ins_expense_account": "3",
            "salaries_payable_account": "5", "tax_payable_account": "6",
            "insurance_payable_account": "7"
        })
        self.assertEqual(response.status_code, 302)
        conn = sqlite3.connect(self.db_path)
        status = conn.execute("SELECT status FROM payroll_runs WHERE id=1").fetchone()[0]
        journal_count = conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0]
        conn.close()
        self.assertEqual(status, "posted")
        self.assertEqual(journal_count, 1)

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


if __name__ == "__main__":
    unittest.main()
