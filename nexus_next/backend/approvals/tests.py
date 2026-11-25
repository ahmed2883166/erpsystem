from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock

from approvals.snapshots import invoice_discount_snapshot


class InvoiceDiscountSnapshotTests(TestCase):
    def make_invoice(self, *, discount_percent="12.5000", unit_price="100.0000"):
        tax_code = SimpleNamespace(pk=9, code="VAT-STD", category="standard")
        product = SimpleNamespace(pk=3, code="ITEM-03", name_ar="صنف تجريبي")
        delivery_line = SimpleNamespace(pk=11, product=product, quantity="2.0000")
        line = SimpleNamespace(
            pk=17,
            delivery_line_id=11,
            delivery_line=delivery_line,
            unit_price=unit_price,
            tax_code=tax_code,
            tax_rate="14.0000",
            discount_percent=discount_percent,
            discount_amount="25.0000",
            net_amount="175.0000",
            tax_amount="24.5000",
            total_amount="199.5000",
        )
        ordered = Mock()
        ordered.order_by.return_value = [line]
        selected = Mock()
        selected.order_by.return_value = [line]
        lines = Mock()
        lines.select_related.return_value = selected
        invoice = SimpleNamespace(lines=lines)
        return invoice

    def test_snapshot_contains_each_line_and_tax_discount_details(self):
        snapshot = invoice_discount_snapshot(self.make_invoice())
        self.assertEqual(len(snapshot), 1)
        self.assertEqual(snapshot[0]["delivery_line_id"], 11)
        self.assertEqual(snapshot[0]["product_code"], "ITEM-03")
        self.assertEqual(snapshot[0]["discount_percent"], "12.5000")
        self.assertEqual(snapshot[0]["tax_code"], "VAT-STD")
        self.assertEqual(snapshot[0]["total_amount"], "199.5000")

    def test_snapshot_changes_when_pricing_or_discount_changes(self):
        approved = invoice_discount_snapshot(self.make_invoice())
        changed_discount = invoice_discount_snapshot(self.make_invoice(discount_percent="15.0000"))
        changed_price = invoice_discount_snapshot(self.make_invoice(unit_price="120.0000"))
        self.assertNotEqual(approved, changed_discount)
        self.assertNotEqual(approved, changed_price)
