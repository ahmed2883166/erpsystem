import unittest

from tax_categories import (
    tax_category_options,
    tax_category_label,
    validate_tax_category,
)


class TaxCategoryTests(unittest.TestCase):
    def test_egypt_customs_import_category_keeps_standard_input_tax_treatment(self):
        code = validate_tax_category(
            "EG", "purchase", "eg_purchase_customs_vat", "standard"
        )
        self.assertEqual(code, "eg_purchase_customs_vat")

    def test_saudi_sales_and_purchases_are_country_scoped(self):
        self.assertTrue(tax_category_options("SA", "sale"))
        self.assertTrue(tax_category_options("SA", "purchase"))
        with self.assertRaises(ValueError):
            validate_tax_category("EG", "sale", "sa_sales_standard", "standard")

    def test_egypt_and_saudi_purchase_categories_are_country_scoped(self):
        egypt = {item.code: item for item in tax_category_options("EG", "purchase")}
        saudi = {item.code: item for item in tax_category_options("SA", "purchase")}
        self.assertIn("eg_purchase_customs_vat", egypt)
        self.assertIn("sa_purchase_customs_vat", saudi)
        self.assertEqual(egypt["eg_purchase_customs_vat"].treatment, "standard")
        self.assertEqual(saudi["sa_purchase_customs_vat"].treatment, "standard")
        self.assertNotIn("sa_purchase_reverse_charge", saudi)
        self.assertNotIn("eg_purchase_reverse_charge", egypt)
        self.assertNotIn("eg_purchase_table_tax", egypt)

    def test_zero_and_exempt_purchase_categories_match_their_tax_treatment(self):
        self.assertEqual(
            validate_tax_category("EG", "purchase", "eg_purchase_zero", "zero_rated"),
            "eg_purchase_zero",
        )
        self.assertEqual(
            validate_tax_category("SA", "purchase", "sa_purchase_exempt", "exempt"),
            "sa_purchase_exempt",
        )
        with self.assertRaises(ValueError):
            validate_tax_category("EG", "purchase", "eg_purchase_exempt", "standard")

    def test_category_must_match_existing_generic_tax_treatment(self):
        with self.assertRaises(ValueError):
            validate_tax_category("SA", "sale", "sa_sales_zero_export", "standard")

    def test_legacy_blank_category_is_preserved(self):
        self.assertEqual(validate_tax_category("EG", "sale", "", "standard"), "")
        self.assertEqual(tax_category_label("EG", "sale", ""), "")

    def test_unsupported_reverse_charge_is_not_offered_as_a_normal_purchase(self):
        codes = {item.code for item in tax_category_options("SA", "purchase")}
        self.assertNotIn("sa_purchase_reverse_charge", codes)


if __name__ == "__main__":
    unittest.main()
