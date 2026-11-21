from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from companies.models import Branch, Company, CompanyMembership, Currency
from .models import Product, ProductCategory, UnitOfMeasure, Warehouse


class InventoryApiTests(APITestCase):
    def setUp(self):
        currency = Currency.objects.create(code="EGP", name_ar="جنيه مصري")
        self.company = Company.objects.create(code="INV", name_ar="شركة المخزون", country="EG",
                                              activity="commercial", base_currency=currency)
        self.other_company = Company.objects.create(code="INV2", name_ar="شركة أخرى", country="SA",
                                                    activity="commercial", base_currency=currency)
        self.branch = Branch.objects.create(company=self.company, code="B1", name_ar="الفرع الرئيسي")
        self.user = get_user_model().objects.create_user(username="inventory_admin", password="test-password")
        CompanyMembership.objects.create(user=self.user, company=self.company, role="admin", all_branches=True)
        self.client.force_authenticate(self.user)
        self.unit = UnitOfMeasure.objects.create(company=self.company, code="PCS", name_ar="قطعة")
        self.category = ProductCategory.objects.create(company=self.company, code="CAT", name_ar="عام")

    def test_product_create_and_company_scope(self):
        hidden_unit = UnitOfMeasure.objects.create(company=self.other_company, code="PCS", name_ar="قطعة")
        hidden = Product.objects.create(company=self.other_company, unit=hidden_unit, code="P1", name_ar="صنف مخفي")
        response = self.client.get("/api/inventory/products/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 0) if isinstance(response.data, dict) and "count" in response.data else self.assertEqual(len(response.data), 0)
        created = self.client.post("/api/inventory/products/", {
            "company": self.company.pk, "unit": self.unit.pk, "category": self.category.pk,
            "code": " p-001 ", "barcode": "123456", "name_ar": "صنف تجريبي",
            "purchase_price": "10.2500", "sale_price": "15.0000",
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        self.assertEqual(created.data["code"], "P-001")
        self.assertNotEqual(hidden.pk, created.data["id"])

    def test_product_rejects_foreign_company_unit(self):
        foreign_unit = UnitOfMeasure.objects.create(company=self.other_company, code="BOX", name_ar="كرتونة")
        response = self.client.post("/api/inventory/products/", {
            "company": self.company.pk, "unit": foreign_unit.pk, "code": "P-002", "name_ar": "غير صحيح"
        }, format="json")
        self.assertEqual(response.status_code, 400)

    def test_barcode_unique_within_company(self):
        Product.objects.create(company=self.company, unit=self.unit, code="P-003", barcode="9988", name_ar="أول")
        response = self.client.post("/api/inventory/products/", {
            "company": self.company.pk, "unit": self.unit.pk, "code": "P-004",
            "barcode": "9988", "name_ar": "ثاني",
        }, format="json")
        self.assertEqual(response.status_code, 400)

    def test_warehouse_branch_must_belong_to_company(self):
        foreign_branch = Branch.objects.create(company=self.other_company, code="B2", name_ar="فرع آخر")
        response = self.client.post("/api/inventory/warehouses/", {
            "company": self.company.pk, "branch": foreign_branch.pk,
            "code": "WH1", "name_ar": "مخزن غير صحيح",
        }, format="json")
        self.assertEqual(response.status_code, 400)

    def test_viewer_cannot_create_inventory_product(self):
        viewer = get_user_model().objects.create_user(username="inventory_viewer", password="test-password")
        CompanyMembership.objects.create(user=viewer, company=self.company, role="viewer", all_branches=True)
        self.client.force_authenticate(viewer)
        response = self.client.post("/api/inventory/products/", {
            "company": self.company.pk, "unit": self.unit.pk, "code": "P-005", "name_ar": "ممنوع"
        }, format="json")
        self.assertEqual(response.status_code, 403)
