import unittest
from decimal import Decimal

from purchases import _allocate_supplier_bill_return_value


class SupplierReturnInvoicePricingTests(unittest.TestCase):
    def test_allocates_return_to_original_invoice_prices(self):
        value, allocations = _allocate_supplier_bill_return_value(
            [
                {"bill_line_id": 11, "billed_quantity": "10", "allocated_quantity": "0",
                 "invoice_unit_price": "10.00"},
                {"bill_line_id": 12, "billed_quantity": "5", "allocated_quantity": "0",
                 "invoice_unit_price": "12.00"},
            ],
            Decimal("12"),
        )
        self.assertEqual(value, Decimal("124.00"))
        self.assertEqual(
            [(item["supplier_bill_line_id"], item["quantity"], item["total_value"])
             for item in allocations],
            [(11, Decimal("10"), Decimal("100.00")),
             (12, Decimal("2"), Decimal("24.00"))],
        )

    def test_legacy_returns_reduce_oldest_invoice_line_before_new_allocation(self):
        value, allocations = _allocate_supplier_bill_return_value(
            [
                {"bill_line_id": 21, "billed_quantity": "10", "allocated_quantity": "0",
                 "invoice_unit_price": "10"},
                {"bill_line_id": 22, "billed_quantity": "5", "allocated_quantity": "0",
                 "invoice_unit_price": "12"},
            ],
            Decimal("8"),
            legacy_returned_quantity=Decimal("3"),
        )
        self.assertEqual(value, Decimal("82.00"))
        self.assertEqual(
            [(item["supplier_bill_line_id"], item["quantity"]) for item in allocations],
            [(21, Decimal("7")), (22, Decimal("1"))],
        )

    def test_rejects_return_above_remaining_billed_quantity(self):
        with self.assertRaisesRegex(ValueError, "تتجاوز الكمية المفوترة"):
            _allocate_supplier_bill_return_value(
                [{"bill_line_id": 31, "billed_quantity": "2", "allocated_quantity": "1",
                  "invoice_unit_price": "5"}],
                Decimal("2"),
            )

    def test_rejects_corrupt_legacy_return_allocation(self):
        with self.assertRaisesRegex(ValueError, "مرتجعات المورد القديمة"):
            _allocate_supplier_bill_return_value(
                [{"bill_line_id": 41, "billed_quantity": "2", "allocated_quantity": "0",
                  "invoice_unit_price": "5"}],
                Decimal("1"),
                legacy_returned_quantity=Decimal("3"),
            )


if __name__ == "__main__":
    unittest.main()
