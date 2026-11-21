from datetime import date
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from .models import Branch, Company, Currency, FiscalYear

class CompanyFoundationTests(TestCase):
    def setUp(self):
        self.egp = Currency.objects.create(code="EGP", name_ar="جنيه مصري", symbol="ج.م")
        self.company = Company.objects.create(
            code="NEX-EG", name_ar="شركة تجريبية", country=Company.Country.EGYPT,
            activity=Company.Activity.COMMERCIAL, base_currency=self.egp,
        )

    def test_company_supports_only_configured_countries_and_activities(self):
        self.assertEqual(self.company.country, "EG")
        self.assertEqual(len(Company.Country.choices), 2)
        self.assertNotIn("manufacturing", dict(Company.Activity.choices))

    def test_branch_code_is_unique_per_company(self):
        Branch.objects.create(company=self.company, code="MAIN", name_ar="الرئيسي")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Branch.objects.create(company=self.company, code="MAIN", name_ar="مكرر")

    def test_only_one_head_office_per_company(self):
        Branch.objects.create(company=self.company, code="HQ", name_ar="المركز", is_head_office=True)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Branch.objects.create(company=self.company, code="HQ2", name_ar="مركز آخر", is_head_office=True)

    def test_fiscal_year_rejects_end_before_start(self):
        year = FiscalYear(
            company=self.company, name="FY 2026",
            start_date=date(2026, 12, 31), end_date=date(2026, 1, 1),
        )
        with self.assertRaises(ValidationError):
            year.full_clean()


from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

class CompanySetupApiTests(APITestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="reader", password="test-password")
        Currency.objects.create(code="SAR", name_ar="ريال سعودي")

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
