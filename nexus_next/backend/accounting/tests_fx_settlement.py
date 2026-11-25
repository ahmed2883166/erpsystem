from decimal import Decimal

from django.test import SimpleTestCase

from .fx_settlement import (
    allocate_carrying_base_amount,
    calculate_realized_fx,
)


class FxSettlementArithmeticTests(SimpleTestCase):
    def test_partial_settlement_allocates_historical_carrying_value(self):
        self.assertEqual(
            allocate_carrying_base_amount("100.0000", "500.0000", "30.0000"),
            Decimal("150.0000"),
        )

    def test_final_settlement_absorbs_rounding_residual(self):
        remaining_base = Decimal("333.3334")
        self.assertEqual(
            allocate_carrying_base_amount("66.6667", remaining_base, "66.6667"),
            remaining_base,
        )

    def test_settlement_cannot_exceed_remaining_foreign_amount(self):
        with self.assertRaises(ValueError):
            allocate_carrying_base_amount("10", "50", "10.0001")

    def test_receivable_exchange_difference_signs(self):
        self.assertEqual(
            calculate_realized_fx("receivable", "100", "110"),
            (Decimal("10.0000"), Decimal("0.0000")),
        )
        self.assertEqual(
            calculate_realized_fx("receivable", "100", "90"),
            (Decimal("0.0000"), Decimal("10.0000")),
        )

    def test_payable_exchange_difference_signs(self):
        self.assertEqual(
            calculate_realized_fx("payable", "100", "90"),
            (Decimal("10.0000"), Decimal("0.0000")),
        )
        self.assertEqual(
            calculate_realized_fx("payable", "100", "110"),
            (Decimal("0.0000"), Decimal("10.0000")),
        )

    def test_zero_difference_does_not_create_gain_or_loss(self):
        self.assertEqual(
            calculate_realized_fx("receivable", "100", "100"),
            (Decimal("0.0000"), Decimal("0.0000")),
        )

    def test_invalid_direction_and_non_positive_amounts_are_rejected(self):
        with self.assertRaises(ValueError):
            calculate_realized_fx("inventory", "100", "100")
        with self.assertRaises(ValueError):
            calculate_realized_fx("payable", "100", "0")
