import unittest
from decimal import Decimal

from receivables import _amount


class ReceivablesDecimalTests(unittest.TestCase):
    def test_large_amount_is_preserved_exactly(self):
        self.assertEqual(
            _amount("123456789012345.67") - _amount("0.02"),
            Decimal("123456789012345.65"),
        )

    def test_non_finite_amount_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "غير محدود"):
            _amount("NaN")

    def test_invalid_amount_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "غير صالح"):
            _amount("abc")


if __name__ == "__main__":
    unittest.main()
