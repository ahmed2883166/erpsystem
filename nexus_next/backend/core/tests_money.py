from decimal import Decimal
from types import SimpleNamespace

from django.test import SimpleTestCase

from core.money import round_currency_amount


class CurrencyRoundingTests(SimpleTestCase):
    def test_rounds_half_up_to_currency_minor_unit(self):
        egp = SimpleNamespace(decimal_places=2)
        self.assertEqual(round_currency_amount(Decimal("1.005"), egp), Decimal("1.01"))

    def test_respects_currency_precision_above_two_places(self):
        unit = SimpleNamespace(decimal_places=3)
        self.assertEqual(round_currency_amount(Decimal("1.2345"), unit), Decimal("1.235"))

    def test_rejects_invalid_currency_precision(self):
        invalid = SimpleNamespace(decimal_places=9)
        with self.assertRaises(ValueError):
            round_currency_amount(Decimal("1.00"), invalid)
