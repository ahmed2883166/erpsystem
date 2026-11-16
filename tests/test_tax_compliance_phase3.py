import unittest
from decimal import Decimal

from tax_center import calculate_tax_input_totals


class TaxInputTotalsTests(unittest.TestCase):
    def test_supplier_credit_note_reduces_gross_and_deductible_input_tax(self):
        documents = [
            {"tax_treatment": "standard", "tax_amount": "140.00", "deductible_percent": "100"},
            {"tax_treatment": "standard", "tax_amount": "70.00", "deductible_percent": "50"},
            # Credit note is represented as a negative tax amount.
            {"tax_treatment": "standard", "tax_amount": "-14.00", "deductible_percent": "100"},
        ]

        gross, deductible, non_deductible = calculate_tax_input_totals(documents)

        self.assertEqual(gross, Decimal("196.00"))
        self.assertEqual(deductible, Decimal("161.00"))
        self.assertEqual(non_deductible, Decimal("35.00"))

    def test_exempt_and_zero_rated_purchases_do_not_create_recoverable_input_tax(self):
        documents = [
            {"tax_treatment": "exempt", "tax_amount": "0", "deductible_percent": "100"},
            {"tax_treatment": "zero_rated", "tax_amount": "0", "deductible_percent": "100"},
            {"tax_treatment": "exempt", "tax_amount": "12.00", "deductible_percent": "100"},
        ]

        gross, deductible, non_deductible = calculate_tax_input_totals(documents)

        self.assertEqual(gross, Decimal("12.00"))
        self.assertEqual(deductible, Decimal("0"))
        self.assertEqual(non_deductible, Decimal("12.00"))

    def test_invalid_deductible_percentage_is_not_claimed(self):
        documents = [
            {"tax_treatment": "standard", "tax_amount": "100.00", "deductible_percent": "125"},
        ]
        gross, deductible, non_deductible = calculate_tax_input_totals(documents)
        self.assertEqual(gross, Decimal("100.00"))
        self.assertEqual(deductible, Decimal("0"))
        self.assertEqual(non_deductible, Decimal("100.00"))

    def test_partial_deductibility_is_split_without_losing_posted_tax(self):
        documents = [
            {"tax_treatment": "standard", "tax_amount": "15.01", "deductible_percent": "33.33"},
        ]

        gross, deductible, non_deductible = calculate_tax_input_totals(documents)

        self.assertEqual(gross, Decimal("15.01"))
        self.assertEqual(deductible, Decimal("5.00"))
        self.assertEqual(non_deductible, Decimal("10.01"))
        self.assertEqual(deductible + non_deductible, gross)


if __name__ == "__main__":
    unittest.main()
