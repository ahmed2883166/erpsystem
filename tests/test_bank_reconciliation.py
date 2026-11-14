import unittest
from decimal import Decimal

from banking import expected_statement_amount


class BankStatementAmountTests(unittest.TestCase):
    def test_deposit_is_positive_for_source_bank(self):
        amount, side = expected_statement_amount({
            "amount": "125.50", "transaction_type": "deposit",
            "bank_account_id": 10, "destination_bank_account_id": None,
        }, 10)
        self.assertEqual(amount, Decimal("125.50"))
        self.assertEqual(side, "source")

    def test_withdrawal_and_fee_are_negative_for_source_bank(self):
        for kind in ("withdrawal", "fee"):
            with self.subTest(kind=kind):
                amount, side = expected_statement_amount({
                    "amount": "40.00", "transaction_type": kind,
                    "bank_account_id": 10, "destination_bank_account_id": None,
                }, 10)
                self.assertEqual(amount, Decimal("-40.00"))
                self.assertEqual(side, "source")

    def test_transfer_has_opposite_signed_sides(self):
        transaction = {
            "amount": "300.00", "transaction_type": "transfer",
            "bank_account_id": 10, "destination_bank_account_id": 20,
        }
        source_amount, source_side = expected_statement_amount(transaction, 10)
        target_amount, target_side = expected_statement_amount(transaction, 20)
        self.assertEqual((source_amount, source_side), (Decimal("-300.00"), "source"))
        self.assertEqual((target_amount, target_side), (Decimal("300.00"), "destination"))

    def test_unrelated_bank_account_is_rejected(self):
        with self.assertRaises(ValueError):
            expected_statement_amount({
                "amount": "10", "transaction_type": "deposit",
                "bank_account_id": 10, "destination_bank_account_id": None,
            }, 99)


if __name__ == "__main__":
    unittest.main()
