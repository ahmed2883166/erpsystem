import sqlite3
import unittest
from unittest.mock import patch
from decimal import Decimal

from payroll_compliance import (calculate_payroll_deductions, seed_egypt_2026, _payroll_obligations, ensure_default_payroll_rules, _lock_posted_payroll_period, upgrade_egypt_2026_tax_tiers_and_recalculate_drafts)


class PayrollComplianceTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE company_settings(setting_key TEXT PRIMARY KEY, setting_value TEXT NOT NULL);
            INSERT INTO company_settings VALUES('tax_country','EG');
            CREATE TABLE payroll_rule_sets(
                id INTEGER PRIMARY KEY, country_code TEXT, category_code TEXT, effective_from TEXT,
                effective_to TEXT, status TEXT DEFAULT 'active', personal_exemption_annual TEXT, taxable_rounding_unit TEXT DEFAULT '1',
                insurance_floor TEXT, insurance_ceiling TEXT,
                employee_insurance_rate TEXT, employer_insurance_rate TEXT,
                enabled INTEGER DEFAULT 1, notes TEXT DEFAULT ''
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


    def test_tax_bracket_union_uses_valid_numeric_ordering(self):
        rule_id = self.add_rule()
        self.conn.execute("""
            INSERT INTO payroll_tax_tier_brackets(rule_set_id,income_max,lower_bound,upper_bound,rate)
            VALUES(?,?,?,?,?)
        """, (rule_id, "600000", "0", "50000", "10"))
        rows = self.conn.execute("""
            SELECT * FROM (
                SELECT b.id,b.rule_set_id,b.lower_bound,b.upper_bound,b.rate,
                       'EG' AS country_code,'local' AS category_code,
                       NULL AS income_max,'standard' AS bracket_kind
                FROM payroll_tax_brackets b WHERE b.rule_set_id=?
                UNION ALL
                SELECT t.id,t.rule_set_id,t.lower_bound,t.upper_bound,t.rate,
                       'EG' AS country_code,'local' AS category_code,
                       t.income_max,'tier' AS bracket_kind
                FROM payroll_tax_tier_brackets t WHERE t.rule_set_id=?
            ) AS all_brackets
            ORDER BY country_code,category_code,bracket_kind,
                     CAST(lower_bound AS NUMERIC),id
        """, (rule_id, rule_id)).fetchall()
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["bracket_kind"], "standard")
        self.assertEqual(rows[-1]["bracket_kind"], "tier")

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


    def test_egypt_preset_never_overwrites_existing_rule_or_tax_brackets(self):
        rule_id = self.add_rule()
        self.conn.execute("""
            INSERT INTO payroll_tax_tier_brackets(rule_set_id,income_max,lower_bound,upper_bound,rate)
            VALUES(?,?,?,?,?)
        """, (rule_id, "500000", "0", "50000", "7"))
        self.conn.commit()

        with self.assertRaisesRegex(ValueError, "موجود بالفعل"):
            seed_egypt_2026(self.conn)

        rule = self.conn.execute(
            "SELECT personal_exemption_annual,employee_insurance_rate FROM payroll_rule_sets WHERE id=?",
            (rule_id,),
        ).fetchone()
        standard_brackets = self.conn.execute(
            "SELECT lower_bound,upper_bound,rate FROM payroll_tax_brackets WHERE rule_set_id=? ORDER BY id",
            (rule_id,),
        ).fetchall()
        tier_brackets = self.conn.execute(
            "SELECT income_max,lower_bound,upper_bound,rate FROM payroll_tax_tier_brackets WHERE rule_set_id=?",
            (rule_id,),
        ).fetchall()
        self.assertEqual(tuple(rule), ("24000", "10"))
        self.assertEqual([tuple(row) for row in standard_brackets], [
            ("0", "60000", "10"), ("60000", None, "20")
        ])
        self.assertEqual([tuple(row) for row in tier_brackets], [("500000", "0", "50000", "7")])

    def test_default_egypt_rules_are_seeded_once_without_overwriting_edits(self):
        ensure_default_payroll_rules(self.conn)
        self.conn.execute(
            "UPDATE payroll_rule_sets SET employee_insurance_rate='10' WHERE country_code='EG'"
        )
        ensure_default_payroll_rules(self.conn)
        rate = self.conn.execute(
            "SELECT employee_insurance_rate FROM payroll_rule_sets WHERE country_code='EG'"
        ).fetchone()[0]
        self.assertEqual(rate, "10")

    def test_egypt_income_over_600k_uses_correct_tax_tier(self):
        seed_egypt_2026(self.conn)
        result = calculate_payroll_deductions(self.conn, self.employee, 60000, "2026-11")
        self.assertEqual(result["taxable_annual"], Decimal("677950.00"))
        self.assertEqual(result["tax"], Decimal("12353.13"))
        self.assertEqual(result["insured_wage"], Decimal("16700.00"))

    def test_egypt_tax_tier_selection_at_every_annual_income_boundary(self):
        seed_egypt_2026(self.conn)
        self.conn.execute("""UPDATE payroll_rule_sets
            SET personal_exemption_annual='0', insurance_floor='0', insurance_ceiling='0',
                employee_insurance_rate='0', employer_insurance_rate='0'
            WHERE country_code='EG' AND category_code='local'""")
        self.employee["insurance_enabled"] = 0
        cases = [
            ("50000.00", Decimal("600000.00"), Decimal("10395.83")),
            ("50001.67", Decimal("600020.00"), Decimal("10729.58")),
            ("58333.33", Decimal("699990.00"), Decimal("12812.29")),
            ("58335.00", Decimal("700020.00"), Decimal("13042.08")),
            ("66666.66", Decimal("799990.00"), Decimal("15124.79")),
            ("66668.34", Decimal("800020.00"), Decimal("15417.08")),
            ("75000.00", Decimal("900000.00"), Decimal("17500.00")),
            ("75001.67", Decimal("900020.00"), Decimal("17917.08")),
            ("100000.00", Decimal("1200000.00"), Decimal("24166.67")),
            ("100001.67", Decimal("1200020.00"), Decimal("25000.46")),
        ]
        for monthly_gross, expected_base, expected_tax in cases:
            with self.subTest(monthly_gross=monthly_gross):
                result = calculate_payroll_deductions(
                    self.conn, self.employee, monthly_gross, "2026-11"
                )
                self.assertEqual(result["taxable_annual"], expected_base)
                self.assertEqual(result["tax"], expected_tax)

    def test_egypt_2026_legacy_production_tiers_upgrade_and_only_draft_lines_recalculate(self):
        rule_id = seed_egypt_2026(self.conn)
        self.conn.execute("""DELETE FROM payroll_tax_tier_brackets
            WHERE rule_set_id=? AND income_max IS NULL""", (rule_id,))
        self.conn.execute("""INSERT INTO payroll_tax_tier_brackets(
            rule_set_id,income_max,lower_bound,upper_bound,rate
        ) VALUES(?,NULL,'0',NULL,'27.5')""", (rule_id,))
        self.conn.executescript("""
            CREATE TABLE employees(
                id INTEGER PRIMARY KEY,legal_category TEXT,insurance_enabled INTEGER
            );
            CREATE TABLE payroll_runs(id INTEGER PRIMARY KEY,period TEXT,status TEXT);
            CREATE TABLE payroll_lines(
                id INTEGER PRIMARY KEY,run_id INTEGER,employee_id INTEGER,
                gross_pay TEXT,absence_deduction TEXT,stamp_duty TEXT,
                advance_deduction TEXT,other_deductions TEXT,penalty_deduction TEXT,
                employer_insurance TEXT,employee_insurance TEXT,taxable_income_tax TEXT,
                total_entitlements TEXT,total_deductions TEXT,net_pay TEXT,
                rule_set_id INTEGER,taxable_annual TEXT,insured_wage TEXT
            );
            INSERT INTO employees VALUES(1,'local',0);
            INSERT INTO payroll_runs VALUES(1,'2026-11','draft');
            INSERT INTO payroll_runs VALUES(2,'2026-11','posted');
            INSERT INTO payroll_lines VALUES
                (1,1,1,'103000','0','0','0','0','0','0','0','27866.67',
                 '103000','27866.67','75133.33',1,'1216000','0'),
                (2,2,1,'103000','0','0','0','0','0','0','0','27866.67',
                 '103000','27866.67','75133.33',1,'1216000','0');
        """)
        result = upgrade_egypt_2026_tax_tiers_and_recalculate_drafts(self.conn)
        self.assertEqual(result["tier_status"], "updated")
        self.assertEqual(result["draft_lines_recalculated"], 1)
        self.assertEqual(result["posted_lines_review_required"], 1)
        draft = self.conn.execute(
            "SELECT taxable_income_tax,net_pay FROM payroll_lines WHERE id=1"
        ).fetchone()
        posted = self.conn.execute(
            "SELECT taxable_income_tax,net_pay FROM payroll_lines WHERE id=2"
        ).fetchone()
        self.assertEqual(draft["taxable_income_tax"], "25366.67")
        self.assertEqual(draft["net_pay"], "77633.33")
        self.assertEqual(posted["taxable_income_tax"], "27866.67")
        self.assertEqual(posted["net_pay"], "75133.33")
        second = upgrade_egypt_2026_tax_tiers_and_recalculate_drafts(self.conn)
        self.assertEqual(second["tier_status"], "already_current")

    def test_egypt_2026_tax_upgrade_does_not_overwrite_custom_brackets(self):
        rule_id = seed_egypt_2026(self.conn)
        self.conn.execute("""UPDATE payroll_tax_tier_brackets SET rate='26'
            WHERE rule_set_id=? AND income_max='1200000' AND lower_bound='400000'""", (rule_id,))
        before = self.conn.execute("""SELECT rate FROM payroll_tax_tier_brackets
            WHERE rule_set_id=? AND income_max='1200000' AND lower_bound='400000'""", (rule_id,)).fetchone()[0]
        result = upgrade_egypt_2026_tax_tiers_and_recalculate_drafts(self.conn)
        after = self.conn.execute("""SELECT rate FROM payroll_tax_tier_brackets
            WHERE rule_set_id=? AND income_max='1200000' AND lower_bound='400000'""", (rule_id,)).fetchone()[0]
        self.assertEqual(result["tier_status"], "custom")
        self.assertEqual(before, after)

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


    def test_statutory_payment_locks_posted_payroll_period_on_postgresql(self):
        with patch("payroll_compliance.database_dialect", return_value="postgresql"), \
             patch("payroll_compliance._execute") as execute:
            _lock_posted_payroll_period(object(), "2026-11")
        self.assertIn("FOR UPDATE", execute.call_args.args[1])
        self.assertEqual(execute.call_args.args[2], ("2026-11",))

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


    def test_egypt_highest_income_tier_applies_25_percent_to_first_1_2m_and_27_5_above(self):
        rule_id = seed_egypt_2026(self.conn)
        self.conn.execute("""
            UPDATE payroll_rule_sets
               SET personal_exemption_annual='0',
                   insurance_floor='0',insurance_ceiling='0',
                   employee_insurance_rate='0',employer_insurance_rate='0'
             WHERE id=?
        """, (rule_id,))
        self.conn.execute(
            "DELETE FROM payroll_tax_tier_brackets WHERE rule_set_id=? AND income_max IS NULL",
            (rule_id,),
        )
        self.conn.executemany("""
            INSERT INTO payroll_tax_tier_brackets(rule_set_id,income_max,lower_bound,upper_bound,rate)
            VALUES(?,?,?,?,?)
        """, [
            (rule_id, None, "0", "1200000", "25"),
            (rule_id, None, "1200000", None, "27.5"),
        ])
        self.employee["insurance_enabled"] = 0

        cases = [
            ("100000", Decimal("1200000.00"), Decimal("24166.67")),
            ("100000.08", Decimal("1200000.00"), Decimal("24166.67")),
            ("100000.84", Decimal("1200010.00"), Decimal("25000.23")),
            ("166666.67", Decimal("2000000.00"), Decimal("43333.33")),
        ]
        for monthly_gross, expected_base, expected_monthly_tax in cases:
            with self.subTest(monthly_gross=monthly_gross):
                result = calculate_payroll_deductions(
                    self.conn, self.employee, monthly_gross, "2026-11"
                )
                self.assertEqual(result["taxable_annual"], expected_base)
                self.assertEqual(result["tax"], expected_monthly_tax)


if __name__ == "__main__":
    unittest.main()