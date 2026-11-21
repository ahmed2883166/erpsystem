from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from accounting.models import ChartOfAccount, JournalEntry
from companies.models import Branch, Company, CompanyMembership, Currency, FiscalYear


class FixedAssetWorkflowTests(APITestCase):
    def setUp(self):
        currency = Currency.objects.create(code="EGP", name_ar="جنيه مصري")
        self.company = Company.objects.create(code="AST", name_ar="شركة الأصول", country="EG", activity="commercial", base_currency=currency)
        self.branch = Branch.objects.create(company=self.company, code="MAIN", name_ar="الرئيسي", is_head_office=True)
        self.year = FiscalYear.objects.create(company=self.company, name="2026", start_date=date(2026, 1, 1), end_date=date(2026, 12, 31))
        user = get_user_model().objects.create_user(username="asset-user", password="test-password")
        CompanyMembership.objects.create(user=user, company=self.company, role="accountant", all_branches=True)
        self.client.force_authenticate(user)
        self.asset_gl = self._account("1500", "الأصول الثابتة", "asset")
        self.accum_gl = self._account("1590", "مجمع الإهلاك", "asset")
        self.expense_gl = self._account("6100", "مصروف الإهلاك", "expense")
        self.cash_gl = self._account("1100", "البنك", "asset")
        self.gain_loss_gl = self._account("7100", "أرباح وخسائر بيع أصول", "revenue")

    def _account(self, code, name, nature):
        return ChartOfAccount.objects.create(company=self.company, code=code, name_ar=name, nature=nature)

    def _payload(self):
        return {
            "company": self.company.pk, "branch": self.branch.pk, "code": "FA-001", "name_ar": "سيارة الشركة",
            "acquisition_date": "2026-01-10", "depreciation_start_date": "2026-01-10",
            "original_cost": "120000.0000", "salvage_value": "12000.0000", "useful_life_months": 36,
            "asset_account": self.asset_gl.pk, "accumulated_depreciation_account": self.accum_gl.pk,
            "depreciation_expense_account": self.expense_gl.pk, "acquisition_counter_account": self.cash_gl.pk,
            "disposal_gain_loss_account": self.gain_loss_gl.pk, "fiscal_year": self.year.pk,
        }

    def test_acquisition_posts_asset_cost_and_monthly_depreciation(self):
        created = self.client.post("/api/assets/fixed-assets/", self._payload(), format="json")
        self.assertEqual(created.status_code, 201, created.data)
        asset_id = created.data["id"]
        self.assertEqual(JournalEntry.objects.count(), 1)
        acquisition = JournalEntry.objects.get(source_type=JournalEntry.Source.ASSETS)
        self.assertEqual(acquisition.status, JournalEntry.Status.POSTED)
        self.assertEqual(acquisition.lines.get(account=self.asset_gl).debit, Decimal("120000.0000"))
        depreciation = self.client.post(f"/api/assets/fixed-assets/{asset_id}/depreciate/", {
            "period_date": "2026-02-28", "fiscal_year": self.year.pk,
        }, format="json")
        self.assertEqual(depreciation.status_code, 201, depreciation.data)
        self.assertEqual(depreciation.data["depreciation_amount"], "3000.0000")
        schedule = self.client.get(f"/api/assets/fixed-assets/{asset_id}/schedule/")
        self.assertEqual(schedule.status_code, 200, schedule.data)
        self.assertEqual(schedule.data["accumulated_depreciation"], "3000.0000")
        self.assertEqual(schedule.data["net_book_value"], "117000.0000")
        self.assertEqual(JournalEntry.objects.count(), 2)

    def test_asset_acquisition_is_atomic_when_fiscal_year_is_invalid(self):
        payload = self._payload()
        payload["fiscal_year"] = 999999
        failed = self.client.post("/api/assets/fixed-assets/", payload, format="json")
        self.assertNotEqual(failed.status_code, 201)
        self.assertFalse(__import__("assets.models", fromlist=["FixedAsset"]).FixedAsset.objects.filter(code="FA-001").exists())
        self.assertEqual(JournalEntry.objects.count(), 0)

    def test_duplicate_depreciation_period_is_rejected(self):
        created = self.client.post("/api/assets/fixed-assets/", self._payload(), format="json")
        self.assertEqual(created.status_code, 201, created.data)
        url = f"/api/assets/fixed-assets/{created.data['id']}/depreciate/"
        payload = {"period_date": "2026-02-28", "fiscal_year": self.year.pk}
        first = self.client.post(url, payload, format="json")
        second = self.client.post(url, {"period_date": "2026-02-15", "fiscal_year": self.year.pk}, format="json")
        self.assertEqual(first.status_code, 201, first.data)
        self.assertEqual(second.status_code, 400)
        self.assertEqual(JournalEntry.objects.count(), 2)
