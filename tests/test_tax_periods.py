import unittest
from datetime import date

from tax_center import _period


class TaxPeriodParsingTests(unittest.TestCase):
    def test_monthly_period_keeps_existing_range(self):
        period, start, end = _period("2026-11", country="EG")
        self.assertEqual(period, "2026-11")
        self.assertEqual(start, date(2026, 11, 1))
        self.assertEqual(end, date(2026, 12, 1))

    def test_saudi_quarter_resolves_to_three_month_half_open_range(self):
        period, start, end = _period("2026-Q3", country="SA")
        self.assertEqual(period, "2026-Q3")
        self.assertEqual(start, date(2026, 7, 1))
        self.assertEqual(end, date(2026, 10, 1))

    def test_fourth_quarter_crosses_year_boundary(self):
        _, start, end = _period("2026-Q4", country="SA")
        self.assertEqual(start, date(2026, 10, 1))
        self.assertEqual(end, date(2027, 1, 1))

    def test_egypt_rejects_quarterly_period(self):
        with self.assertRaisesRegex(ValueError, "الإقرار المصري"):
            _period("2026-Q1", country="EG")

    def test_invalid_period_is_rejected(self):
        for value in ("2026-Q5", "2026-13", "26-Q1", "2026-1"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    _period(value, country="SA")


if __name__ == "__main__":
    unittest.main()
