from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from companies.models import Company, CompanyMembership, Currency
from .models import Supplier


class SupplierApiTests(APITestCase):
    def setUp(self):
        currency = Currency.objects.create(code="EGP", name_ar="جنيه مصري")
        self.company = Company.objects.create(code="SUP", name_ar="شركة الموردين", country="EG", activity="commercial", base_currency=currency)
        self.other_company = Company.objects.create(code="SUP2", name_ar="شركة أخرى", country="SA", activity="service", base_currency=currency)
        self.user = get_user_model().objects.create_user(username="supplier_accountant", password="test-password")
        CompanyMembership.objects.create(user=self.user, company=self.company, role="accountant", all_branches=True)
        self.client.force_authenticate(self.user)

    def test_supplier_list_is_company_scoped(self):
        own = Supplier.objects.create(company=self.company, code="S-001", name_ar="مورد الشركة")
        hidden = Supplier.objects.create(company=self.other_company, code="S-001", name_ar="مورد آخر")
        response = self.client.get("/api/purchases/suppliers/")
        self.assertEqual(response.status_code, 200)
        ids = [row["id"] for row in response.data]
        self.assertEqual(ids, [own.pk])
        self.assertNotIn(hidden.pk, ids)

    def test_accountant_can_create_supplier(self):
        response = self.client.post("/api/purchases/suppliers/", {
            "company": self.company.pk, "code": " s-002 ", "name_ar": "مورد جديد",
            "supplier_type": "company", "credit_limit": "10000.00", "payment_terms_days": 30,
        }, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertTrue(Supplier.objects.filter(company=self.company, code="S-002").exists())

    def test_viewer_cannot_create_supplier(self):
        viewer = get_user_model().objects.create_user(username="supplier_viewer", password="test-password")
        CompanyMembership.objects.create(user=viewer, company=self.company, role="viewer", all_branches=True)
        self.client.force_authenticate(viewer)
        response = self.client.post("/api/purchases/suppliers/", {
            "company": self.company.pk, "code": "S-003", "name_ar": "غير مسموح",
        }, format="json")
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Supplier.objects.filter(company=self.company, code="S-003").exists())

    def test_supplier_cannot_be_created_in_unassigned_company(self):
        response = self.client.post("/api/purchases/suppliers/", {
            "company": self.other_company.pk, "code": "S-004", "name_ar": "شركة أخرى",
        }, format="json")
        self.assertEqual(response.status_code, 403)
