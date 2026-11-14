import unittest
from decimal import Decimal

from banking import signed_ledger_amount


class BankStatementAmountTests(unittest.TestCase):
    def test_debit_is_positive_in_bank_statement_direction(self):
        self.assertEqual(signed_ledger_amount("125.50", "0"), Decimal("125.50"))

    def test_credit_is_negative_in_bank_statement_direction(self):
        self.assertEqual(signed_ledger_amount("0", "40.00"), Decimal("-40.00"))

    def test_signed_amount_uses_exact_decimal_arithmetic(self):
        self.assertEqual(signed_ledger_amount("100.10", "0.01"), Decimal("100.09"))

    def test_empty_values_are_treated_as_zero(self):
        self.assertEqual(signed_ledger_amount(None, None), Decimal("0.00"))


if __name__ == "__main__":
    unittest.main()
