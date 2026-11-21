from rest_framework.test import APITestCase
from django.contrib.auth import get_user_model

from companies.models import Company, CompanyMembership, Currency
from .models import Customer


class CustomerApiTests(APITestCase):
    def setUp(self):
        self.currency = Currency.objects.create(code="EGP", name_ar="جنيه مصري")
        self.company = Company.objects.create(
            code="CUS", name_ar="شركة العملاء", country="EG",
            activity="commercial", base_currency=self.currency,
        )
        self.other_company = Company.objects.create(
            code="OTHER", name_ar="شركة أخرى", country="SA",
            activity="service", base_currency=self.currency,
        )
        self.user = get_user_model().objects.create_user(
            username="customer_accountant", password="test-password"
        )
        CompanyMembership.objects.create(
            user=self.user, company=self.company, role="accountant", all_branches=True
        )
        self.client.force_authenticate(self.user)

    def test_customer_list_is_company_scoped(self):
        own = Customer.objects.create(
            company=self.company, code="C-001", name_ar="عميل الشركة"
        )
        hidden = Customer.objects.create(
            company=self.other_company, code="C-001", name_ar="عميل آخر"
        )
        response = self.client.get("/api/sales/customers/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["id"] for row in response.data], [own.pk])
        self.assertNotIn(hidden.pk, [row["id"] for row in response.data])

    def test_accountant_can_create_customer(self):
        response = self.client.post("/api/sales/customers/", {
            "company": self.company.pk,
            "code": " c-002 ",
            "customer_type": "company",
            "name_ar": "عميل جديد",
            "credit_limit": "25000.00",
            "payment_terms_days": 30,
        }, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertTrue(Customer.objects.filter(
            company=self.company, code="C-002", name_ar="عميل جديد"
        ).exists())

    def test_viewer_cannot_create_customer(self):
        viewer = get_user_model().objects.create_user(
            username="customer_viewer", password="test-password"
        )
        CompanyMembership.objects.create(
            user=viewer, company=self.company, role="viewer", all_branches=True
        )
        self.client.force_authenticate(viewer)
        response = self.client.post("/api/sales/customers/", {
            "company": self.company.pk, "code": "C-003", "name_ar": "غير مسموح"
        }, format="json")
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Customer.objects.filter(company=self.company, code="C-003").exists())

    def test_customer_cannot_be_created_in_unassigned_company(self):
        response = self.client.post("/api/sales/customers/", {
            "company": self.other_company.pk, "code": "C-004", "name_ar": "شركة أخرى"
        }, format="json")
        self.assertEqual(response.status_code, 403)
