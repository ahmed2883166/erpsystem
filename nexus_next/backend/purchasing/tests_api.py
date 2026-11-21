from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from accounting.models import AccountRole, ChartOfAccount, JournalEntry, JournalLine
from companies.models import Branch, Company, CompanyMembership, Currency, FiscalYear
from inventory.models import Product, StockBalance, UnitOfMeasure, Warehouse
from suppliers.models import Supplier


class PurchasingWorkflowTests(APITestCase):
    def setUp(self):
        currency = Currency.objects.create(code="EGP", name_ar="جنيه مصري")
        self.company = Company.objects.create(code="BUY", name_ar="شركة مشتريات", country="EG", activity="commercial", base_currency=currency)
        self.branch = Branch.objects.create(company=self.company, code="MAIN", name_ar="الرئيسي", is_head_office=True)
        self.year = FiscalYear.objects.create(company=self.company, name="2026", start_date=date(2026, 1, 1), end_date=date(2026, 12, 31))
        self.user = get_user_model().objects.create_user(username="buyer", password="test-password")
        CompanyMembership.objects.create(user=self.user, company=self.company, role="accountant", all_branches=True)
        self.client.force_authenticate(self.user)
        self.supplier = Supplier.objects.create(company=self.company, code="SUP-1", name_ar="المورد الأول")
        self.unit = UnitOfMeasure.objects.create(company=self.company, code="PCS", name_ar="قطعة")
        self.product = Product.objects.create(company=self.company, unit=self.unit, code="P-1", name_ar="صنف 1", purchase_price=Decimal("100"))
        self.warehouse = Warehouse.objects.create(company=self.company, branch=self.branch, code="WH1", name_ar="المخزن الرئيسي")
        roles = {
            AccountRole.Role.INVENTORY: ("1000", "المخزون", ChartOfAccount.Nature.ASSET),
            AccountRole.Role.GOODS_RECEIVED_NOT_INVOICED: ("2100", "بضاعة مستلمة غير مفوترة", ChartOfAccount.Nature.LIABILITY),
            AccountRole.Role.ACCOUNTS_PAYABLE: ("2200", "الموردون", ChartOfAccount.Nature.LIABILITY),
            AccountRole.Role.VAT_INPUT: ("1300", "ضريبة مدخلات", ChartOfAccount.Nature.ASSET),
        }
        for role, (code, name, nature) in roles.items():
            account = ChartOfAccount.objects.create(company=self.company, code=code, name_ar=name, nature=nature)
            AccountRole.objects.create(company=self.company, role=role, account=account)

    def _create_and_post_receipt(self):
        response = self.client.post("/api/purchases/receipts/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "warehouse": self.warehouse.pk,
            "receipt_number": "GR-001", "receipt_date": "2026-03-10",
            "lines": [{"product": self.product.pk, "quantity": "2", "unit_cost": "100"}],
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        receipt_id = response.data["id"]
        response = self.client.post(f"/api/purchases/receipts/{receipt_id}/post/", {}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        return receipt_id

    def test_receipt_posts_inventory_and_grni_once_then_invoice_clears_grni(self):
        receipt_id = self._create_and_post_receipt()
        balance = StockBalance.objects.get(product=self.product, warehouse=self.warehouse)
        self.assertEqual(balance.quantity, Decimal("2"))
        self.assertEqual(balance.inventory_value, Decimal("200.0000"))
        self.assertEqual(JournalEntry.objects.filter(source_type=JournalEntry.Source.INVENTORY, status="posted").count(), 1)

        response = self.client.post("/api/purchases/invoices/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "receipt": receipt_id,
            "invoice_number": "INV-S-001", "invoice_date": "2026-03-10",
            "lines": [{"receipt_line": 1, "unit_price": "100", "tax_rate": "14"}],
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        invoice_id = response.data["id"]
        response = self.client.post(f"/api/purchases/invoices/{invoice_id}/post/", {}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["net_total"], "200.0000")
        self.assertEqual(response.data["tax_total"], "28.0000")
        self.assertEqual(response.data["grand_total"], "228.0000")
        self.assertEqual(StockBalance.objects.get(product=self.product, warehouse=self.warehouse).quantity, Decimal("2"))
        self.assertEqual(JournalEntry.objects.filter(source_type=JournalEntry.Source.PURCHASES, status="posted").count(), 1)
        purchase_lines = list(JournalLine.objects.filter(entry__source_type=JournalEntry.Source.PURCHASES))\n        self.assertEqual(sum((line.debit for line in purchase_lines), Decimal("0")), Decimal("228.0000"))\n        self.assertEqual(sum((line.credit for line in purchase_lines), Decimal("0")), Decimal("228.0000"))

    def test_invoice_rejects_price_mismatch_against_grni(self):
        receipt_id = self._create_and_post_receipt()
        response = self.client.post("/api/purchases/invoices/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "receipt": receipt_id,
            "invoice_number": "INV-S-002", "invoice_date": "2026-03-10",
            "lines": [{"receipt_line": 1, "unit_price": "95", "tax_rate": "14"}],
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        response = self.client.post(f"/api/purchases/invoices/{response.data['id']}/post/", {}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(StockBalance.objects.get(product=self.product, warehouse=self.warehouse).quantity, Decimal("2"))
        self.assertFalse(JournalEntry.objects.filter(source_type=JournalEntry.Source.PURCHASES).exists())

    def test_invoice_cannot_be_created_before_receipt_is_posted(self):
        response = self.client.post("/api/purchases/receipts/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "warehouse": self.warehouse.pk,
            "receipt_number": "GR-002", "receipt_date": "2026-03-11",
            "lines": [{"product": self.product.pk, "quantity": "1", "unit_cost": "100"}],
        }, format="json")
        self.assertEqual(response.status_code, 201)
        response = self.client.post("/api/purchases/invoices/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "receipt": response.data["id"],
            "invoice_number": "INV-S-003", "invoice_date": "2026-03-11",
            "lines": [{"receipt_line": 2, "unit_price": "100", "tax_rate": "0"}],
        }, format="json")
        self.assertEqual(response.status_code, 400)

    def test_viewer_cannot_post_receipt(self):
        receipt_response = self.client.post("/api/purchases/receipts/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "warehouse": self.warehouse.pk,
            "receipt_number": "GR-003", "receipt_date": "2026-03-12",
            "lines": [{"product": self.product.pk, "quantity": "1", "unit_cost": "100"}],
        }, format="json")
        self.assertEqual(receipt_response.status_code, 201)
        viewer = get_user_model().objects.create_user(username="buyer_viewer", password="test-password")
        CompanyMembership.objects.create(user=viewer, company=self.company, role="viewer", all_branches=True)
        self.client.force_authenticate(viewer)
        response = self.client.post(f"/api/purchases/receipts/{receipt_response.data['id']}/post/", {}, format="json")
        self.assertEqual(response.status_code, 403)
