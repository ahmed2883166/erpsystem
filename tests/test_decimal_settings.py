import unittest
from decimal import Decimal

from decimal_settings import parse_decimal_setting


class DecimalSettingTests(unittest.TestCase):
    def test_preserves_long_decimal_precision(self):
        value = "0.123456789012345678901234567890123456789"
        self.assertEqual(parse_decimal_setting(value), Decimal(value))

    def test_accepts_decimal_exponents_without_float_conversion(self):
        self.assertEqual(parse_decimal_setting("1e-30"), Decimal("1e-30"))

    def test_accepts_inclusive_bounds(self):
        self.assertEqual(parse_decimal_setting("0", minimum=0, maximum=100), Decimal("0"))
        self.assertEqual(parse_decimal_setting("100", minimum=0, maximum=100), Decimal("100"))

    def test_rejects_values_outside_bounds(self):
        for value in ("-0.0001", "100.0001"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_decimal_setting(value, minimum=0, maximum=100)

    def test_rejects_invalid_and_non_finite_values(self):
        for value in ("", "not-a-number", "NaN", "Infinity", "-Infinity", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_decimal_setting(value)

    def test_rejects_boolean_values(self):
        with self.assertRaises(ValueError):
            parse_decimal_setting(True)


if __name__ == "__main__":
    unittest.main()
