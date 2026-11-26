from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from accounting.models import ChartOfAccount
from assets.models import AssetMovement, FixedAsset
from assets.services import depreciate_asset
from companies.models import Branch, Company, CompanyMembership, Currency, FiscalYear
from hr.models import Employee
from treasury.models import TreasuryAccount


class AssetMovementWorkflowTests(APITestCase):
    def setUp(self):
        currency = Currency.objects.create(code="EGP", name_ar="جنيه مصري")
        self.company = Company.objects.create(
            code="AMV", name_ar="شركة حركة الأصول", country="EG",
            activity="commercial", base_currency=currency,
        )
        self.branch = Branch.objects.create(
            company=self.company, code="MAIN", name_ar="الرئيسي", is_head_office=True,
        )
        self.target_branch = Branch.objects.create(
            company=self.company, code="WEST", name_ar="فرع غرب",
        )
        self.year = FiscalYear.objects.create(
            company=self.company, name="2026", start_date=date(2026, 1, 1),
            end_date=date(2026, 12, 31),
        )
        self.user = get_user_model().objects.create_user(username="asset-movement-user", password="test-password")
        CompanyMembership.objects.create(user=self.user, company=self.company, role="accountant", all_branches=True)
        self.manager = get_user_model().objects.create_user(username="asset-movement-manager", password="test-password")
        CompanyMembership.objects.create(user=self.manager, company=self.company, role="admin", all_branches=True)
        self.client.force_authenticate(self.user)
        self.asset_gl = self._account("1500", "الأصول الثابتة", "asset")
        self.accum_gl = self._account("1590", "مجمع الإهلاك", "asset")
        self.expense_gl = self._account("6100", "مصروف الإهلاك", "expense")
        self.cash_gl = self._account("1100", "البنك", "asset")
        TreasuryAccount.objects.create(
            company=self.company, branch=self.branch, account=self.cash_gl,
            kind=TreasuryAccount.Kind.BANK, code="BANK-AMV", name_ar="بنك الأصول",
            opening_balance=Decimal("200000"),
        )
        self.gain_gl = self._account("7100", "أرباح بيع الأصول", "revenue")
        self.employee = Employee.objects.create(
            company=self.company, code="EMP-001", full_name_ar="أحمد المستلم",
            branch=self.target_branch, hire_date=date(2025, 1, 1), currency=currency,
        )
        created = self.client.post("/api/assets/fixed-assets/", {
            "company": self.company.pk, "branch": self.branch.pk, "code": "FA-MOVE-01",
            "name_ar": "سيارة نقل", "custodian": "مسؤول سابق",
            "acquisition_date": "2026-01-10", "depreciation_start_date": "2026-01-10",
            "original_cost": "120000.0000", "salvage_value": "12000.0000",
            "useful_life_months": 36, "asset_account": self.asset_gl.pk,
            "accumulated_depreciation_account": self.accum_gl.pk,
            "depreciation_expense_account": self.expense_gl.pk,
            "acquisition_counter_account": self.cash_gl.pk,
            "disposal_gain_loss_account": self.gain_gl.pk,
            "disposal_loss_account": self.expense_gl.pk, "fiscal_year": self.year.pk,
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        self.asset = FixedAsset.objects.get(pk=created.data["id"])

    def _account(self, code, name, nature):
        return ChartOfAccount.objects.create(company=self.company, code=code, name_ar=name, nature=nature)


    def test_duplicate_document_number_is_rejected_without_creating_second_movement(self):
        payload = {
            "asset": self.asset.pk, "movement_type": "branch_transfer",
            "movement_date": "2026-02-05", "to_branch": self.target_branch.pk,
            "to_custodian": "مسؤول فرع غرب", "document_number": "AM-DUP-01",
        }
        first = self.client.post("/api/assets/asset-movements/", payload, format="json")
        self.assertEqual(first.status_code, 201, first.data)
        second = self.client.post("/api/assets/asset-movements/", payload, format="json")
        self.assertEqual(second.status_code, 400, second.data)
        self.assertEqual(AssetMovement.objects.filter(document_number="AM-DUP-01").count(), 1)

    def test_branch_transfer_requires_admin_approval_and_keeps_immutable_history(self):
        requested = self.client.post("/api/assets/asset-movements/", {
            "asset": self.asset.pk, "movement_type": "branch_transfer",
            "movement_date": "2026-02-01", "to_branch": self.target_branch.pk,
            "to_employee": self.employee.pk, "document_number": "AM-0001",
            "notes": "نقل عهدة السيارة إلى فرع غرب",
        }, format="json")
        self.assertEqual(requested.status_code, 201, requested.data)
        movement_id = requested.data["id"]
        self.asset.refresh_from_db()
        self.assertEqual(self.asset.branch_id, self.branch.pk)
        self.assertEqual(requested.data["status"], AssetMovement.Status.PENDING)
        self.assertEqual(requested.data["from_custodian"], "مسؤول سابق")

        self.client.force_authenticate(self.manager)
        approved = self.client.post(f"/api/assets/asset-movements/{movement_id}/approve/", {
            "review_note": "تمت مراجعة محضر التسليم والاستلام",
        }, format="json")
        self.assertEqual(approved.status_code, 200, approved.data)
        self.asset.refresh_from_db()
        self.assertEqual(self.asset.branch_id, self.target_branch.pk)
        self.assertEqual(self.asset.custodian_employee_id, self.employee.pk)
        self.assertEqual(self.asset.custodian, self.employee.full_name_ar)
        movement = AssetMovement.objects.get(pk=movement_id)
        self.assertEqual(movement.status, AssetMovement.Status.APPROVED)
        self.assertEqual(movement.from_branch_id, self.branch.pk)
        self.assertEqual(movement.to_branch_id, self.target_branch.pk)
        with self.assertRaises(Exception):
            movement.delete()

    def test_rejected_transfer_does_not_change_asset_branch(self):
        requested = self.client.post("/api/assets/asset-movements/", {
            "asset": self.asset.pk, "movement_type": "branch_transfer",
            "movement_date": "2026-02-02", "to_branch": self.target_branch.pk,
            "to_custodian": "مسؤول فرع غرب", "document_number": "AM-0002",
        }, format="json")
        self.assertEqual(requested.status_code, 201, requested.data)
        self.client.force_authenticate(self.manager)
        rejected = self.client.post(
            f"/api/assets/asset-movements/{requested.data['id']}/reject/",
            {"review_note": "بيانات التسليم غير مكتملة"}, format="json",
        )
        self.assertEqual(rejected.status_code, 200, rejected.data)
        self.asset.refresh_from_db()
        self.assertEqual(self.asset.branch_id, self.branch.pk)
        self.assertEqual(rejected.data["status"], AssetMovement.Status.REJECTED)

    def test_employee_must_belong_to_destination_branch(self):
        other_employee = Employee.objects.create(
            company=self.company, code="EMP-002", full_name_ar="موظف الفرع الحالي",
            branch=self.branch, hire_date=date(2025, 1, 1), currency=self.company.base_currency,
        )
        rejected = self.client.post("/api/assets/asset-movements/", {
            "asset": self.asset.pk, "movement_type": "branch_transfer",
            "movement_date": "2026-02-03", "to_branch": self.target_branch.pk,
            "to_employee": other_employee.pk, "to_custodian": "مستلم",
            "document_number": "AM-0003",
        }, format="json")
        self.assertEqual(rejected.status_code, 400, rejected.data)

    def test_custody_change_updates_employee_link_after_approval(self):
        requested = self.client.post("/api/assets/asset-movements/", {
            "asset": self.asset.pk, "movement_type": "custody_change",
            "movement_date": "2026-02-04", "to_employee": self.employee.pk,
            "to_branch": self.branch.pk, "document_number": "AM-0004",
        }, format="json")
        self.assertEqual(requested.status_code, 400, requested.data)
        employee_here = Employee.objects.create(
            company=self.company, code="EMP-003", full_name_ar="موظف الرئيسي",
            branch=self.branch, hire_date=date(2025, 1, 1), currency=self.company.base_currency,
        )
        requested = self.client.post("/api/assets/asset-movements/", {
            "asset": self.asset.pk, "movement_type": "custody_change",
            "movement_date": "2026-02-04", "to_employee": employee_here.pk,
            "document_number": "AM-0005",
        }, format="json")
        self.assertEqual(requested.status_code, 201, requested.data)
        self.client.force_authenticate(self.manager)
        approved = self.client.post(
            f"/api/assets/asset-movements/{requested.data['id']}/approve/", {}, format="json",
        )
        self.assertEqual(approved.status_code, 200, approved.data)
        self.asset.refresh_from_db()
        self.assertEqual(self.asset.custodian_employee_id, employee_here.pk)
        self.assertEqual(self.asset.custodian, employee_here.full_name_ar)
        self.assertEqual(self.asset.branch_id, self.branch.pk)

    def test_depreciation_is_split_at_approved_branch_transfer_and_reconciles(self):
        requested = self.client.post("/api/assets/asset-movements/", {
            "asset": self.asset.pk, "movement_type": "branch_transfer",
            "movement_date": "2026-02-15", "to_branch": self.target_branch.pk,
            "to_custodian": "مسؤول فرع غرب", "document_number": "AM-DEP-SPLIT-01",
        }, format="json")
        self.assertEqual(requested.status_code, 201, requested.data)

        self.client.force_authenticate(self.manager)
        approved = self.client.post(
            f"/api/assets/asset-movements/{requested.data['id']}/approve/", {}, format="json",
        )
        self.assertEqual(approved.status_code, 200, approved.data)

        amount = depreciate_asset(self.asset.pk, date(2026, 2, 28), self.year.pk, self.manager)
        rows = list(self.asset.depreciations.select_related("journal_entry").order_by("journal_entry__branch_id"))
        self.assertEqual(amount, Decimal("5000.0000"))
        self.assertEqual(len(rows), 2)
        self.assertEqual(sum((row.amount for row in rows), Decimal("0")), amount)
        self.assertTrue(all(row.is_partial_period for row in rows))
        by_branch = {row.journal_entry.branch_id: row.amount for row in rows}
        self.assertEqual(by_branch[self.branch.pk], Decimal("3600.0000"))
        self.assertEqual(by_branch[self.target_branch.pk], Decimal("1400.0000"))

        reconciliation = self.client.get(
            "/api/assets/fixed-assets/account-reconciliation/",
            {"company": self.company.pk, "branch": self.target_branch.pk,
             "as_of": "2026-02-28", "date_from": "2026-01-01", "date_to": "2026-02-28"},
        )
        self.assertEqual(reconciliation.status_code, 200, reconciliation.data)
        self.assertTrue(reconciliation.data["is_reconciled"], reconciliation.data)
        self.assertEqual(reconciliation.data["difference_count"], 0)

    def test_backdated_transfer_is_rejected_after_depreciation_is_posted(self):
        depreciate_asset(self.asset.pk, date(2026, 2, 28), self.year.pk, self.user)
        response = self.client.post("/api/assets/asset-movements/", {
            "asset": self.asset.pk, "movement_type": "branch_transfer",
            "movement_date": "2026-02-15", "to_branch": self.target_branch.pk,
            "to_custodian": "مسؤول فرع غرب", "document_number": "AM-BACKDATE-01",
        }, format="json")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(AssetMovement.objects.filter(document_number="AM-BACKDATE-01").exists())

