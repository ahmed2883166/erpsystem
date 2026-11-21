import unittest

from tax_categories import (
    tax_category_options,
    tax_category_label,
    validate_tax_category,
)


class TaxCategoryTests(unittest.TestCase):
    def test_egypt_sales_exposes_table_tax_as_separate_internal_category(self):
        options = tax_category_options("EG", "sale")
        table_tax = next(item for item in options if item.code == "eg_sales_table_tax")
        self.assertEqual(table_tax.treatment, "standard")
        self.assertIn("نسبة جدول معتمدة", table_tax.note_ar)

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
