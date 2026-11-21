import unittest
from decimal import Decimal

from tax_mechanisms import (
    calculate_egypt_table_tax,
    calculate_saudi_reverse_charge,
    build_saudi_reverse_charge_journal_lines,
)


class EgyptTableTaxTests(unittest.TestCase):
    def test_percentage_table_tax_is_separate_from_vat(self):
        result = calculate_egypt_table_tax("T2", "1000", rate="5")
        self.assertEqual(result["tax_amount"], Decimal("50.00"))
        self.assertEqual(result["tax_type"], "T2")
        self.assertIsNone(result["quantity"])

    def test_fixed_table_tax_uses_quantity_not_taxable_base(self):
        result = calculate_egypt_table_tax(
            "T3", "1000", quantity="10", amount_per_unit="2.50"
        )
        self.assertEqual(result["tax_amount"], Decimal("25.00"))
        self.assertEqual(result["tax_type"], "T3")

    def test_table_tax_rejects_unknown_type_and_negative_inputs(self):
        with self.assertRaises(ValueError):
            calculate_egypt_table_tax("T1", "1000", rate="14")
        with self.assertRaises(ValueError):
            calculate_egypt_table_tax("T2", "1000", rate="-1")
        with self.assertRaises(ValueError):
            calculate_egypt_table_tax("T3", "1000", quantity="-1", amount_per_unit="2")


class SaudiReverseChargeTests(unittest.TestCase):
    def test_fully_deductible_reverse_charge_reports_output_and_input(self):
        result = calculate_saudi_reverse_charge("SA", "1000", "15", "100")
        self.assertEqual(result["output_tax"], Decimal("150.00"))
        self.assertEqual(result["recoverable_input_tax"], Decimal("150.00"))
        self.assertEqual(result["non_recoverable_input_tax"], Decimal("0.00"))
        self.assertEqual(result["net_vat_effect"], Decimal("0.00"))

    def test_partially_deductible_reverse_charge_keeps_nonrecoverable_part(self):
        result = calculate_saudi_reverse_charge("SA", "1000", "15", "60")
        self.assertEqual(result["output_tax"], Decimal("150.00"))
        self.assertEqual(result["recoverable_input_tax"], Decimal("90.00"))
        self.assertEqual(result["non_recoverable_input_tax"], Decimal("60.00"))
        self.assertEqual(result["net_vat_effect"], Decimal("60.00"))

    def test_reverse_charge_rejects_wrong_country_and_invalid_deduction(self):
        with self.assertRaises(ValueError):
            calculate_saudi_reverse_charge("EG", "1000", "15", "100")
        with self.assertRaises(ValueError):
            calculate_saudi_reverse_charge("SA", "1000", "15", "101")


    def test_reverse_charge_journal_plan_is_balanced_and_separates_components(self):
        calculation = calculate_saudi_reverse_charge("SA", "1000", "15", "60")
        lines = build_saudi_reverse_charge_journal_lines(calculation, 301, 302, 601)
        self.assertEqual(
            [(line["account_id"], line["debit"], line["credit"]) for line in lines],
            [
                (302, Decimal("90.00"), Decimal("0.00")),
                (601, Decimal("60.00"), Decimal("0.00")),
                (301, Decimal("0.00"), Decimal("150.00")),
            ],
        )
        self.assertEqual(
            sum((line["debit"] for line in lines), Decimal("0.00")),
            sum((line["credit"] for line in lines), Decimal("0.00")),
        )

    def test_reverse_charge_journal_plan_requires_accounts_for_nonzero_legs(self):
        calculation = calculate_saudi_reverse_charge("SA", "1000", "15", "100")
        with self.assertRaises(ValueError):
            build_saudi_reverse_charge_journal_lines(calculation, 301, None, None)
        partial = calculate_saudi_reverse_charge("SA", "1000", "15", "60")
        with self.assertRaises(ValueError):
            build_saudi_reverse_charge_journal_lines(partial, 301, 302, None)

    def test_reverse_charge_journal_plan_rejects_unbalanced_calculation(self):
        calculation = calculate_saudi_reverse_charge("SA", "1000", "15", "60")
        calculation["recoverable_input_tax"] = Decimal("89.00")
        with self.assertRaises(ValueError):
            build_saudi_reverse_charge_journal_lines(calculation, 301, 302, 601)

    def test_reverse_charge_rounds_each_reportable_tax_amount(self):
        result = calculate_saudi_reverse_charge("SA", "10.03", "15", "50")
        self.assertEqual(result["output_tax"], Decimal("1.50"))
        self.assertEqual(result["recoverable_input_tax"], Decimal("0.75"))
        self.assertEqual(result["non_recoverable_input_tax"], Decimal("0.75"))


if __name__ == "__main__":
    unittest.main()
