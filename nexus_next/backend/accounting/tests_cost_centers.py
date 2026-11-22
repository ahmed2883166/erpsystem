from rest_framework.test import APIClient
from django.test import TestCase

from accounts.models import User
from companies.models import Company, CompanyMembership, Currency
from accounting.models import CostCenter


class CostCenterApiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        currency = Currency.objects.create(code="EGP", name_ar="جنيه مصري")
        cls.company = Company.objects.create(
            code="CC-TST", name_ar="شركة مراكز التكلفة", country="EG",
            activity="commercial", base_currency=currency,
        )
        cls.other_company = Company.objects.create(
            code="CC-OTH", name_ar="شركة أخرى", country="EG",
            activity="commercial", base_currency=currency,
        )
        cls.user = User.objects.create_user(username="cost-center-accountant", password="test-password")
        CompanyMembership.objects.create(
            user=cls.user, company=cls.company, role="accountant", all_branches=True,
        )

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_create_and_list_cost_centers_for_company(self):
        response = self.client.post("/api/accounting/cost-centers/", {
            "company": self.company.pk, "code": "ADMIN", "name_ar": "الإدارة العامة",
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        listing = self.client.get(f"/api/accounting/cost-centers/?company={self.company.pk}")
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(len(listing.data), 1)
        self.assertEqual(listing.data[0]["code"], "ADMIN")

    def test_update_cannot_create_parent_cycle(self):
        root = CostCenter.objects.create(company=self.company, code="ROOT", name_ar="رئيسي")
        child = CostCenter.objects.create(company=self.company, code="CHILD", name_ar="فرعي", parent=root)
        response = self.client.patch(
            f"/api/accounting/cost-centers/{root.pk}/",
            {"parent": child.pk}, format="json",
        )
        self.assertEqual(response.status_code, 400, response.data)
        root.refresh_from_db()
        self.assertIsNone(root.parent_id)

    def test_cost_center_parent_must_belong_to_same_company(self):
        parent = CostCenter.objects.create(company=self.other_company, code="OTHER", name_ar="مركز آخر")
        response = self.client.post("/api/accounting/cost-centers/", {
            "company": self.company.pk, "code": "LOCAL", "name_ar": "مركز محلي", "parent": parent.pk,
        }, format="json")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(CostCenter.objects.filter(company=self.company, code="LOCAL").exists())
