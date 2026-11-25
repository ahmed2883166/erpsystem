from rest_framework.test import APITestCase
from django.contrib.auth import get_user_model

from companies.models import Company, CompanyMembership, Currency
from .models import Customer, CustomerDiscountRule


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

    def test_customer_can_be_updated_and_deactivated_without_deleting_history(self):
        customer = Customer.objects.create(
            company=self.company, code="C-EDIT", name_ar="عميل قبل التعديل"
        )
        updated = self.client.patch(
            f"/api/sales/customers/{customer.pk}/",
            {"name_ar": "عميل بعد التعديل", "is_active": False},
            format="json",
        )
        self.assertEqual(updated.status_code, 200, updated.data)
        self.assertEqual(updated.data["name_ar"], "عميل بعد التعديل")
        self.assertFalse(updated.data["is_active"])
        customer.refresh_from_db()
        self.assertFalse(customer.is_active)
        deleted = self.client.delete(f"/api/sales/customers/{customer.pk}/")
        self.assertEqual(deleted.status_code, 403, getattr(deleted, "data", None))
        self.assertTrue(Customer.objects.filter(pk=customer.pk).exists())

    def test_customer_discount_profile_is_saved_and_validated(self):
        response = self.client.post("/api/sales/customers/", {
            "company": self.company.pk,
            "code": "C-DISCOUNT",
            "name_ar": "عميل بخصم",
            "default_discount_percent": "4.5000",
            "discount_approval_limit": "6.0000",
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["default_discount_percent"], "4.5000")
        self.assertEqual(response.data["discount_approval_limit"], "6.0000")

        invalid = self.client.post("/api/sales/customers/", {
            "company": self.company.pk,
            "code": "C-DISCOUNT-BAD",
            "name_ar": "خصم غير صالح",
            "default_discount_percent": "8",
            "discount_approval_limit": "5",
        }, format="json")
        self.assertEqual(invalid.status_code, 400, invalid.data)
        self.assertFalse(Customer.objects.filter(company=self.company, code="C-DISCOUNT-BAD").exists())

    def test_discount_rules_are_company_scoped_and_accountant_can_manage_them(self):
        response = self.client.post("/api/sales/customer-discount-rules/", {
            "company": self.company.pk,
            "customer_type": "company",
            "discount_percent": "6.2500",
            "priority": 20,
            "effective_from": "2026-01-01",
            "notes": "خصم شركات",
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["discount_percent"], "6.2500")

        hidden = CustomerDiscountRule.objects.create(
            company=self.other_company, discount_percent="9.0000",
            effective_from="2026-01-01",
        )
        listed = self.client.get("/api/sales/customer-discount-rules/?company=%s" % self.company.pk)
        self.assertEqual(listed.status_code, 200)
        self.assertEqual([row["id"] for row in listed.data], [response.data["id"]])
        self.assertNotIn(hidden.pk, [row["id"] for row in listed.data])

    def test_viewer_cannot_manage_discount_rules(self):
        viewer = get_user_model().objects.create_user(
            username="discount_rule_viewer", password="test-password"
        )
        CompanyMembership.objects.create(
            user=viewer, company=self.company, role="viewer", all_branches=True
        )
        self.client.force_authenticate(viewer)
        response = self.client.post("/api/sales/customer-discount-rules/", {
            "company": self.company.pk, "discount_percent": "5",
            "effective_from": "2026-01-01",
        }, format="json")
        self.assertEqual(response.status_code, 403)
        self.assertFalse(CustomerDiscountRule.objects.filter(company=self.company).exists())
