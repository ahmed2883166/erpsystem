import sqlite3
import unittest
from decimal import Decimal

from payroll_compliance import calculate_payroll_deductions


class PayrollComplianceTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE company_settings(setting_key TEXT PRIMARY KEY, setting_value TEXT NOT NULL);
            INSERT INTO company_settings VALUES('tax_country','EG');
            CREATE TABLE payroll_rule_sets(
                id INTEGER PRIMARY KEY, country_code TEXT, category_code TEXT, effective_from TEXT,
                personal_exemption_annual TEXT, insurance_floor TEXT, insurance_ceiling TEXT,
                employee_insurance_rate TEXT, employer_insurance_rate TEXT, enabled INTEGER
            );
            CREATE TABLE payroll_tax_brackets(
                id INTEGER PRIMARY KEY, rule_set_id INTEGER, lower_bound TEXT, upper_bound TEXT, rate TEXT
            );
        """)
        self.employee = {
            "legal_category": "local",
            "insured_wage": "0",
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
        self.assertEqual(result["tax"], Decimal("1100.00"))
        self.assertEqual(result["employee_insurance"], Decimal("1000.00"))
        self.assertEqual(result["employer_insurance"], Decimal("1500.00"))
        self.assertEqual(result["taxable_annual"], Decimal("96000.00"))
        self.assertEqual(result["insured_wage"], Decimal("10000.00"))

    def test_no_effective_rule_uses_manual_fallback(self):
        self.add_rule()
        self.assertIsNone(calculate_payroll_deductions(self.conn, self.employee, 10000, "2025-12"))

    def test_enabled_rule_without_tax_brackets_is_blocked(self):
        self.add_rule(with_brackets=False)
        with self.assertRaisesRegex(ValueError, "لا توجد شرائح"):
            calculate_payroll_deductions(self.conn, self.employee, 10000, "2026-11")


if __name__ == "__main__":
    unittest.main()
