import sqlite3
import unittest
from decimal import Decimal

from payroll_compliance import calculate_payroll_deductions, seed_egypt_2026, _payroll_obligations


class PayrollComplianceTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE company_settings(setting_key TEXT PRIMARY KEY, setting_value TEXT NOT NULL);
            INSERT INTO company_settings VALUES('tax_country','EG');
            CREATE TABLE payroll_rule_sets(
                id INTEGER PRIMARY KEY, country_code TEXT, category_code TEXT, effective_from TEXT,
                personal_exemption_annual TEXT, taxable_rounding_unit TEXT DEFAULT '1',
                insurance_floor TEXT, insurance_ceiling TEXT,
                employee_insurance_rate TEXT, employer_insurance_rate TEXT, enabled INTEGER
            );
            CREATE TABLE payroll_tax_brackets(
                id INTEGER PRIMARY KEY, rule_set_id INTEGER, lower_bound TEXT, upper_bound TEXT, rate TEXT
            );
            CREATE TABLE payroll_tax_tier_brackets(
                id INTEGER PRIMARY KEY, rule_set_id INTEGER, income_max TEXT,
                lower_bound TEXT, upper_bound TEXT, rate TEXT
            );
        """)
        self.employee = {
            "legal_category": "local",
            "insured_wage": "0",
            "insurance_enabled": 1,
        }

    def tearDown(self):
        self.conn.close()

    def add_rule(self, with_brackets=True):
        cur = self.conn.execute("""
            INSERT INTO payroll_rule_sets(country_code,category_code,effective_from,
                personal_exemption_annual,insurance_floor,insurance_ceiling,
                employee_insurance_rate,employer_insurance_rate,enabled)
            VALUES('EG','local','2026-01-01','24000','1000','10000','10','15',1)
        """)
        rule_id = cur.lastrowid
        if with_brackets:
            self.conn.executemany("""
                INSERT INTO payroll_tax_brackets(rule_set_id,lower_bound,upper_bound,rate)
                VALUES(?,?,?,?)
            """, [(rule_id,"0","60000","10"),(rule_id,"60000",None,"20")])
        return rule_id

    def test_progressive_tax_and_insurance_caps(self):
        self.add_rule()
        result = calculate_payroll_deductions(self.conn, self.employee, Decimal("10000"), "2026-11")
        self.assertEqual(result["tax"], Decimal("900.00"))
        self.assertEqual(result["employee_insurance"], Decimal("1000.00"))
        self.assertEqual(result["employer_insurance"], Decimal("1500.00"))
        self.assertEqual(result["taxable_annual"], Decimal("84000.00"))
        self.assertEqual(result["insured_wage"], Decimal("10000.00"))


    def test_egypt_2026_preset_is_loaded_and_calculates(self):
        seed_egypt_2026(self.conn)
        result = calculate_payroll_deductions(self.conn, self.employee, 10000, "2026-11")
        self.assertEqual(result["employee_insurance"], Decimal("1100.00"))
        self.assertEqual(result["employer_insurance"], Decimal("1875.00"))
        self.assertEqual(result["taxable_annual"], Decimal("86800.00"))
        self.assertEqual(result["tax"], Decimal("592.50"))


    def test_egypt_income_over_600k_uses_correct_tax_tier(self):
        seed_egypt_2026(self.conn)
        result = calculate_payroll_deductions(self.conn, self.employee, 60000, "2026-11")
        self.assertEqual(result["taxable_annual"], Decimal("677950.00"))
        self.assertEqual(result["tax"], Decimal("12353.13"))
        self.assertEqual(result["insured_wage"], Decimal("16700.00"))

    def test_no_effective_rule_uses_manual_fallback(self):
        self.add_rule()
        self.assertIsNone(calculate_payroll_deductions(self.conn, self.employee, 10000, "2025-12"))


    def test_employee_not_enrolled_has_no_social_insurance_deduction(self):
        self.add_rule()
        self.employee["insurance_enabled"] = 0
        result = calculate_payroll_deductions(self.conn, self.employee, 10000, "2026-11")
        self.assertEqual(result["employee_insurance"], Decimal("0.00"))
        self.assertEqual(result["employer_insurance"], Decimal("0.00"))
        self.assertEqual(result["taxable_annual"], Decimal("96000.00"))


    def test_payroll_obligations_subtract_recorded_payments(self):
        self.conn.executescript("""
            CREATE TABLE payroll_runs(id INTEGER PRIMARY KEY,period TEXT,status TEXT);
            CREATE TABLE payroll_lines(run_id INTEGER,taxable_income_tax TEXT,
                employee_insurance TEXT,employer_insurance TEXT);
            CREATE TABLE payroll_statutory_payments(period TEXT,payment_type TEXT,amount TEXT);
            INSERT INTO payroll_runs VALUES(1,'2026-11','posted');
            INSERT INTO payroll_lines VALUES(1,'500','1100','1875');
            INSERT INTO payroll_statutory_payments VALUES('2026-11','salary_tax','200');
            INSERT INTO payroll_statutory_payments VALUES('2026-11','social_insurance','1000');
        """)
        result = _payroll_obligations(self.conn, "2026-11")
        self.assertEqual(result["salary_tax"]["remaining"], Decimal("300.00"))
        self.assertEqual(result["social_insurance"]["due"], Decimal("2975.00"))
        self.assertEqual(result["social_insurance"]["remaining"], Decimal("1975.00"))

    def test_enabled_rule_without_tax_brackets_is_blocked(self):
        self.add_rule(with_brackets=False)
        with self.assertRaisesRegex(ValueError, "لا توجد شرائح"):
            calculate_payroll_deductions(self.conn, self.employee, 10000, "2026-11")


if __name__ == "__main__":
    unittest.main()
