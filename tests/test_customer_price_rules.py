import unittest
from decimal import Decimal

from sales import _select_customer_price_rule


class CustomerPriceRuleTests(unittest.TestCase):
    def test_highest_quantity_tier_wins_over_newer_lower_tier(self):
        rules = [
            {'id': 2, 'min_qty': '1', 'unit_price': '100', 'valid_from': '2026-11-10'},
            {'id': 1, 'min_qty': '10', 'unit_price': '80', 'valid_from': '2026-10-01'},
        ]
        self.assertEqual(_select_customer_price_rule(rules, Decimal('10')), '80')

    def test_newer_rule_wins_when_quantity_threshold_is_equal(self):
        rules = [
            {'id': 1, 'min_qty': '5', 'unit_price': '90', 'valid_from': '2026-10-01'},
            {'id': 2, 'min_qty': '5', 'unit_price': '85', 'valid_from': '2026-11-01'},
        ]
        self.assertEqual(_select_customer_price_rule(rules, '5'), '85')

    def test_no_matching_tier_returns_none(self):
        rules = [{'id': 1, 'min_qty': '10', 'unit_price': '80', 'valid_from': '2026-10-01'}]
        self.assertIsNone(_select_customer_price_rule(rules, '9.99'))


if __name__ == '__main__':
    unittest.main()
