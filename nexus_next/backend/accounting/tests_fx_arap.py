from decimal import Decimal
from django.test import SimpleTestCase

from .fx_arap import settle_foreign_balance, to_base_amount, validate_snapshot


class FxArApArithmeticTests(SimpleTestCase):
    def test_foreign_invoice_amount_converts_to_company_base_currency(self):
        self.assertEqual(to_base_amount("100.25", "50.1234567890"), Decimal("5025.1235"))

    def test_snapshot_requires_exact_approved_company_rate(self):
        self.assertEqual(
            validate_snapshot(
                base_currency_code="EGP", currency_code="USD",
                foreign_amount="100", rate="50", rate_date=__import__("datetime").date(2026, 3, 1),
                document_date=__import__("datetime").date(2026, 3, 2), approved_rate="50",
            ),
            Decimal("5000.0000"),
        )

    def test_snapshot_rejects_base_currency_and_unapproved_rate(self):
        common = dict(foreign_amount="10", rate="50", rate_date=__import__("datetime").date(2026, 3, 1),
                      document_date=__import__("datetime").date(2026, 3, 2), approved_rate="50")
        with self.assertRaises(ValueError):
            validate_snapshot(base_currency_code="EGP", currency_code="EGP", **common)
        with self.assertRaises(ValueError):
            validate_snapshot(base_currency_code="EGP", currency_code="USD", **{**common, "rate": "51"})

    def test_snapshot_rejects_future_rate_date(self):
        from datetime import date
        with self.assertRaises(ValueError):
            validate_snapshot(base_currency_code="EGP", currency_code="USD", foreign_amount="10",
                              rate="50", rate_date=date(2026, 3, 3), document_date=date(2026, 3, 2),
                              approved_rate="50")

    def test_receivable_realized_gain(self):
        result = settle_foreign_balance(direction="receivable", remaining_foreign_amount="100",
            remaining_carrying_base_amount="5000", settled_foreign_amount="40", settlement_rate="52")
        self.assertEqual(result.settlement_base_amount, Decimal("2080.0000"))
        self.assertEqual(result.carrying_base_amount, Decimal("2000.0000"))
        self.assertEqual(result.realized_gain, Decimal("80.0000"))
        self.assertEqual(result.realized_loss, Decimal("0.0000"))

    def test_receivable_realized_loss(self):
        result = settle_foreign_balance(direction="receivable", remaining_foreign_amount="100",
            remaining_carrying_base_amount="5000", settled_foreign_amount="40", settlement_rate="48")
        self.assertEqual(result.realized_loss, Decimal("80.0000"))

    def test_payable_gain_when_settled_for_less_base_currency(self):
        result = settle_foreign_balance(direction="payable", remaining_foreign_amount="100",
            remaining_carrying_base_amount="5000", settled_foreign_amount="40", settlement_rate="48")
        self.assertEqual(result.realized_gain, Decimal("80.0000"))
        self.assertEqual(result.realized_loss, Decimal("0.0000"))

    def test_payable_loss_when_settled_for_more_base_currency(self):
        result = settle_foreign_balance(direction="payable", remaining_foreign_amount="100",
            remaining_carrying_base_amount="5000", settled_foreign_amount="40", settlement_rate="52")
        self.assertEqual(result.realized_loss, Decimal("80.0000"))

    def test_final_settlement_consumes_exact_rounding_remainder(self):
        first = settle_foreign_balance(direction="receivable", remaining_foreign_amount="3",
            remaining_carrying_base_amount="100", settled_foreign_amount="1", settlement_rate="33.3333")
        final = settle_foreign_balance(direction="receivable", remaining_foreign_amount="2",
            remaining_carrying_base_amount=first.remaining_carrying_base_amount,
            settled_foreign_amount="2", settlement_rate="33.3333")
        self.assertEqual(first.carrying_base_amount + final.carrying_base_amount, Decimal("100.0000"))
        self.assertEqual(final.remaining_foreign_amount, Decimal("0.0000"))
        self.assertEqual(final.remaining_carrying_base_amount, Decimal("0.0000"))

    def test_over_settlement_and_unknown_direction_are_rejected(self):
        with self.assertRaises(ValueError):
            settle_foreign_balance(direction="receivable", remaining_foreign_amount="10",
                remaining_carrying_base_amount="500", settled_foreign_amount="11", settlement_rate="50")
        with self.assertRaises(ValueError):
            settle_foreign_balance(direction="inventory", remaining_foreign_amount="10",
                remaining_carrying_base_amount="500", settled_foreign_amount="1", settlement_rate="50")
