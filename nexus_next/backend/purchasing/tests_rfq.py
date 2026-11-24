from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from companies.models import Branch, Company, CompanyMembership, Currency
from inventory.models import Product, UnitOfMeasure, Warehouse
from suppliers.models import Supplier


class PurchaseRFQWorkflowTests(APITestCase):
    def setUp(self):
        currency = Currency.objects.create(code="EGP", name_ar="جنيه مصري")
        self.company = Company.objects.create(code="RFQ", name_ar="شركة عروض", country="EG", activity="commercial", base_currency=currency)
        self.branch = Branch.objects.create(company=self.company, code="MAIN", name_ar="الرئيسي", is_head_office=True)
        self.warehouse = Warehouse.objects.create(company=self.company, branch=self.branch, code="WH1", name_ar="المخزن الرئيسي")
        self.user = get_user_model().objects.create_user(username="rfq-buyer", password="test-password")
        self.manager = get_user_model().objects.create_user(username="rfq-manager", password="test-password")
        CompanyMembership.objects.create(user=self.user, company=self.company, role="accountant", all_branches=True)
        CompanyMembership.objects.create(user=self.manager, company=self.company, role="admin", all_branches=True)
        self.client.force_authenticate(self.user)
        self.unit = UnitOfMeasure.objects.create(company=self.company, code="PCS", name_ar="قطعة")
        self.product = Product.objects.create(company=self.company, unit=self.unit, code="RFQ-1", name_ar="صنف منافسة", purchase_price=Decimal("10"))
        self.supplier_a = Supplier.objects.create(company=self.company, code="RFQ-S1", name_ar="مورد أ")
        self.supplier_b = Supplier.objects.create(company=self.company, code="RFQ-S2", name_ar="مورد ب")

    def test_rfq_invite_receive_compare_and_award(self):
        created = self.client.post("/api/purchasing/rfqs/", {
            "company": self.company.pk,
            "rfq_number": "RFQ-2026-001",
            "issue_date": "2026-11-24",
            "closing_date": "2026-11-30",
            "terms": "التوريد للمخزن الرئيسي",
            "invited_suppliers": [self.supplier_a.pk, self.supplier_b.pk],
            "lines": [{"product": self.product.pk, "quantity": "5"}],
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        rfq_id = created.data["id"]
        rfq_line_id = created.data["line_items"][0]["id"]
        opened = self.client.post(f"/api/purchasing/rfqs/{rfq_id}/open/", {}, format="json")
        self.assertEqual(opened.status_code, 200, opened.data)

        quote_a = self.client.post("/api/purchasing/quotations/", {
            "rfq": rfq_id, "supplier": self.supplier_a.pk, "quotation_number": "A-001",
            "quotation_date": "2026-11-24", "valid_until": "2026-12-15",
            "delivery_days": 7, "payment_terms": "30 يوم",
            "lines": [{"rfq_line": rfq_line_id, "unit_price": "12", "discount_percent": "5", "tax_rate": "14"}],
        }, format="json")
        self.assertEqual(quote_a.status_code, 201, quote_a.data)
        quote_b = self.client.post("/api/purchasing/quotations/", {
            "rfq": rfq_id, "supplier": self.supplier_b.pk, "quotation_number": "B-001",
            "quotation_date": "2026-11-24", "valid_until": "2026-12-15",
            "delivery_days": 3, "payment_terms": "نقدي",
            "lines": [{"rfq_line": rfq_line_id, "unit_price": "13", "discount_percent": "0", "tax_rate": "14"}],
        }, format="json")
        self.assertEqual(quote_b.status_code, 201, quote_b.data)
        self.assertEqual(quote_a.data["grand_total"], "64.9800")
        self.client.force_authenticate(self.manager)
        awarded = self.client.post(f"/api/purchasing/rfqs/{rfq_id}/award/", {
            "quotation": quote_a.data["id"], "reason": "أفضل تكلفة إجمالية مع شروط سداد مناسبة"
        }, format="json")
        self.assertEqual(awarded.status_code, 200, awarded.data)
        self.assertEqual(awarded.data["status"], "awarded")
        self.assertEqual(awarded.data["awarded_quotation"], quote_a.data["id"])
        converted = self.client.post(f"/api/purchasing/rfqs/{rfq_id}/convert-award/", {
            "warehouse": self.warehouse.pk, "order_number": "PO-RFQ-001",
            "order_date": "2026-11-24", "expected_date": "2026-12-05",
        }, format="json")
        self.assertEqual(converted.status_code, 201, converted.data)
        order = self.client.get(f"/api/purchasing/orders/{converted.data['purchase_order_id']}/")
        self.assertEqual(order.status_code, 200, order.data)
        self.assertEqual(order.data["line_items"][0]["unit_price"], "11.4000")
        second_award = self.client.post(f"/api/purchasing/rfqs/{rfq_id}/award/", {
            "quotation": quote_b.data["id"], "reason": "محاولة ترسية ثانية"
        }, format="json")
        self.assertEqual(second_award.status_code, 400)
