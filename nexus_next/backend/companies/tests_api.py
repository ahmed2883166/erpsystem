from rest_framework.test import APITestCase
from django.contrib.auth import get_user_model
from .models import Company, CompanyMembership, Currency


class CompanySetupApiTests(APITestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="reader", password="test-password")
        self.currency = Currency.objects.create(code="SAR", name_ar="ريال سعودي")
        self.company = Company.objects.create(
            code="OWN", name_ar="الشركة المصرح بها", country="SA",
            activity="service", base_currency=self.currency,
        )
        self.other_company = Company.objects.create(
            code="OTHER", name_ar="شركة غير مصرح بها", country="EG",
            activity="commercial", base_currency=self.currency,
        )

    def test_setup_endpoints_require_authentication(self):
        response = self.client.get("/api/setup/currencies/")
        self.assertEqual(response.status_code, 403)

    def test_authenticated_user_can_read_currencies(self):
        self.client.force_authenticate(self.user)
        response = self.client.get("/api/setup/currencies/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data[0]["code"], "SAR")

    def test_non_admin_cannot_create_currency(self):
        self.client.force_authenticate(self.user)
        response = self.client.post("/api/setup/currencies/", {
            "code": "AED", "name_ar": "درهم"
        }, format="json")
        self.assertEqual(response.status_code, 403)

    def test_company_list_contains_only_active_memberships(self):
        CompanyMembership.objects.create(user=self.user, company=self.company, role="accountant")
        self.client.force_authenticate(self.user)
        response = self.client.get("/api/setup/companies/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["code"] for row in response.data], ["OWN"])

    def test_company_without_membership_is_not_visible(self):
        self.client.force_authenticate(self.user)
        response = self.client.get("/api/setup/companies/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, [])
