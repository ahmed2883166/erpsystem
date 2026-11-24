from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from assets.models import FixedAsset
from accounting.models import ChartOfAccount, JournalEntry, TaxCode
from approvals.models import DiscountApprovalPolicy, FinancialApprovalRequest
from companies.models import Branch, Company, CompanyMembership, Currency, FiscalYear
from treasury.models import TreasuryAccount, TreasuryTransaction


class FixedAssetWorkflowTests(APITestCase):
    def setUp(self):
        currency = Currency.objects.create(code="EGP", name_ar="جنيه مصري")
        self.company = Company.objects.create(code="AST", name_ar="شركة الأصول", country="EG", activity="commercial", base_currency=currency)
        self.branch = Branch.objects.create(company=self.company, code="MAIN", name_ar="الرئيسي", is_head_office=True)
        self.year = FiscalYear.objects.create(company=self.company, name="2026", start_date=date(2026, 1, 1), end_date=date(2026, 12, 31))
        self.user = get_user_model().objects.create_user(username="asset-user", password="test-password")
        CompanyMembership.objects.create(user=self.user, company=self.company, role="accountant", all_branches=True)
        self.client.force_authenticate(self.user)
        self.asset_gl = self._account("1500", "الأصول الثابتة", "asset")
        self.accum_gl = self._account("1590", "مجمع الإهلاك", "asset")
        self.expense_gl = self._account("6100", "مصروف الإهلاك", "expense")
        self.cash_gl = self._account("1100", "البنك", "asset")
        self.bank = TreasuryAccount.objects.create(company=self.company, branch=self.branch, account=self.cash_gl, kind=TreasuryAccount.Kind.BANK, code="BANK-FA", name_ar="بنك الأصول", opening_balance=Decimal("200000"))
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
            "disposal_gain_loss_account": self.gain_loss_gl.pk, "disposal_loss_account": self.expense_gl.pk, "fiscal_year": self.year.pk,
        }

    def test_acquisition_posts_asset_cost_and_monthly_depreciation(self):
        created = self.client.post("/api/assets/fixed-assets/", self._payload(), format="json")
        self.assertEqual(created.status_code, 201, created.data)
        asset_id = created.data["id"]
        self.assertEqual(JournalEntry.objects.count(), 1)
        acquisition_cash = TreasuryTransaction.objects.get(pk=created.data["acquisition_treasury_transaction"])
        self.assertEqual(acquisition_cash.kind, TreasuryTransaction.Kind.PAYMENT)
        self.assertEqual(acquisition_cash.cash_flow_category, TreasuryTransaction.CashFlowCategory.INVESTING)
        acquisition = JournalEntry.objects.get(source_type=JournalEntry.Source.ASSETS)
        self.assertEqual(acquisition.status, JournalEntry.Status.POSTED)
        self.assertEqual(acquisition.lines.get(account=self.asset_gl).debit, Decimal("120000.0000"))
        depreciation = self.client.post(f"/api/assets/fixed-assets/{asset_id}/depreciate/", {
            "period_date": "2026-02-28", "fiscal_year": self.year.pk,
        }, format="json")
        self.assertEqual(depreciation.status_code, 201, depreciation.data)
        self.assertEqual(depreciation.data["depreciation_amount"], Decimal("5000.0000"))
        schedule = self.client.get(f"/api/assets/fixed-assets/{asset_id}/schedule/")
        self.assertEqual(schedule.status_code, 200, schedule.data)
        self.assertEqual(schedule.data["accumulated_depreciation"], Decimal("5000.0000"))
        self.assertEqual(schedule.data["net_book_value"], Decimal("115000.0000"))
        self.assertEqual(JournalEntry.objects.count(), 2)

    def test_treasury_funded_asset_acquisition_blocks_insufficient_cash(self):
        low_cash_gl = self._account("1110", "بنك برصيد غير كافٍ", "asset")
        TreasuryAccount.objects.create(
            company=self.company, branch=self.branch, account=low_cash_gl,
            kind=TreasuryAccount.Kind.BANK, code="BANK-LOW", name_ar="بنك منخفض الرصيد",
            opening_balance=Decimal("1000"),
        )
        payload = self._payload()
        payload["acquisition_counter_account"] = low_cash_gl.pk
        failed = self.client.post("/api/assets/fixed-assets/", payload, format="json")
        self.assertEqual(failed.status_code, 400, failed.data)
        self.assertIn("الرصيد المتاح غير كافٍ", str(failed.data))
        from assets.models import FixedAsset
        self.assertFalse(FixedAsset.objects.filter(code="FA-001").exists())
        self.assertFalse(TreasuryTransaction.objects.filter(document_number="FA-ACQ-1").exists())
        self.assertFalse(JournalEntry.objects.filter(source_type=JournalEntry.Source.ASSETS).exists())

    def test_treasury_funded_asset_acquisition_requires_and_honors_financial_approval(self):
        DiscountApprovalPolicy.objects.create(
            company=self.company, max_payment_amount=Decimal("50000"),
            created_by=self.user, updated_by=self.user,
        )
        created = self.client.post("/api/assets/fixed-assets/", self._payload(), format="json")
        self.assertEqual(created.status_code, 201, created.data)
        asset_id = created.data["id"]
        transaction_id = created.data["acquisition_treasury_transaction"]
        self.assertIsNone(created.data["acquisition_journal"])
        document = TreasuryTransaction.objects.get(pk=transaction_id)
        self.assertEqual(document.status, TreasuryTransaction.Status.DRAFT)
        approval = FinancialApprovalRequest.objects.get(treasury_transaction=document)
        self.assertEqual(approval.status, FinancialApprovalRequest.Status.PENDING)

        manager = get_user_model().objects.create_user(username="asset-manager", password="test-password")
        CompanyMembership.objects.create(user=manager, company=self.company, role="admin", all_branches=True)
        approval.status = FinancialApprovalRequest.Status.APPROVED
        approval.decided_by = manager
        approval.decision_note = "اعتماد اقتناء الأصل"
        approval.save()

        self.client.force_authenticate(manager)
        posted = self.client.post(f"/api/treasury/transactions/{transaction_id}/post/", {}, format="json")
        self.assertEqual(posted.status_code, 200, posted.data)
        from assets.models import FixedAsset
        asset = FixedAsset.objects.get(pk=asset_id)
        self.assertIsNotNone(asset.acquisition_journal_id)
        self.assertEqual(asset.acquisition_journal.source_type, JournalEntry.Source.ASSETS)
        self.assertEqual(asset.acquisition_journal.status, JournalEntry.Status.POSTED)

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

    def test_sale_posts_partial_daily_depreciation_and_gain(self):
        created = self.client.post("/api/assets/fixed-assets/", self._payload(), format="json")
        self.assertEqual(created.status_code, 201, created.data)
        asset_id = created.data["id"]
        monthly = self.client.post(f"/api/assets/fixed-assets/{asset_id}/depreciate/", {
            "period_date": "2026-02-28", "fiscal_year": self.year.pk,
        }, format="json")
        self.assertEqual(monthly.status_code, 201, monthly.data)
        disposed = self.client.post(f"/api/assets/fixed-assets/{asset_id}/dispose/", {
            "disposal_date": "2026-03-10", "proceeds": "120000.0000",
            "treasury_account": self.bank.pk, "fiscal_year": self.year.pk,
        }, format="json")
        self.assertEqual(disposed.status_code, 201, disposed.data)
        self.assertEqual(disposed.data["accumulated_depreciation"], Decimal("6000.0000"))
        self.assertEqual(disposed.data["net_book_value"], Decimal("114000.0000"))
        self.assertEqual(disposed.data["gain"], Decimal("6000.0000"))
        self.assertEqual(disposed.data["loss"], Decimal("0"))
        from assets.models import AssetDepreciation, FixedAsset
        asset = FixedAsset.objects.get(pk=asset_id)
        self.assertEqual(asset.status, FixedAsset.Status.DISPOSED)
        partial = AssetDepreciation.objects.get(asset=asset, is_partial_period=True)
        self.assertEqual(partial.amount, Decimal("1000.0000"))
        self.assertEqual(JournalEntry.objects.count(), 4)
        disposal_entry = JournalEntry.objects.get(pk=disposed.data["disposal_journal"])
        self.assertEqual(disposal_entry.status, JournalEntry.Status.POSTED)
        self.assertEqual(disposal_entry.lines.get(account=self.asset_gl).credit, Decimal("120000.0000"))
        treasury_doc = TreasuryTransaction.objects.get(pk=disposed.data["treasury_transaction"])
        self.assertEqual(treasury_doc.status, TreasuryTransaction.Status.POSTED)
        self.assertEqual(treasury_doc.journal_entry_id, disposal_entry.pk)
        self.assertEqual(treasury_doc.source_account_id, self.bank.pk)
        self.assertEqual(treasury_doc.cash_flow_category, TreasuryTransaction.CashFlowCategory.INVESTING)
        cash_flow = self.client.get("/api/treasury/transactions/cash-flow/", {
            "company": self.company.pk, "date_from": "2026-01-01", "date_to": "2026-12-31",
        })
        self.assertEqual(cash_flow.status_code, 200, cash_flow.data)
        investing = next(row for row in cash_flow.data["categories"] if row["category"] == "investing")
        self.assertEqual(investing["inflows"], Decimal("120000.0000"))
        self.assertEqual(investing["outflows"], Decimal("120000.0000"))
        self.assertEqual(investing["net"], Decimal("0.0000"))
        register = self.client.get("/api/assets/fixed-assets/register/", {"company": self.company.pk, "as_of": "2026-03-10"})
        self.assertEqual(register.status_code, 200, register.data)
        self.assertEqual(register.data["rows"][0]["status"], "disposed")
        movements = self.client.get("/api/assets/fixed-assets/movement-report/", {"company": self.company.pk})
        self.assertEqual(movements.status_code, 200, movements.data)
        self.assertEqual(len(movements.data["rows"]), 4)
        depreciation_report = self.client.get("/api/assets/fixed-assets/depreciation-report/", {"company": self.company.pk, "date_from": "2026-01-01", "date_to": "2026-12-31"})
        self.assertEqual(depreciation_report.status_code, 200, depreciation_report.data)
        self.assertEqual(depreciation_report.data["total_depreciation"], Decimal("6000.0000"))
        duplicate = self.client.post(f"/api/assets/fixed-assets/{asset_id}/dispose/", {
            "disposal_date": "2026-03-10", "proceeds": "120000.0000",
            "treasury_account": self.bank.pk, "fiscal_year": self.year.pk,
        }, format="json")
        self.assertEqual(duplicate.status_code, 400)
        self.assertEqual(JournalEntry.objects.count(), 4)

    def test_zero_proceeds_disposal_posts_loss_without_fake_treasury_receipt(self):
        payload = self._payload()
        payload["disposal_loss_account"] = self.expense_gl.pk
        created = self.client.post("/api/assets/fixed-assets/", payload, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        asset_id = created.data["id"]
        disposed = self.client.post(f"/api/assets/fixed-assets/{asset_id}/dispose/", {
            "disposal_date": "2026-02-28", "proceeds": "0",
            "fiscal_year": self.year.pk,
        }, format="json")
        self.assertEqual(disposed.status_code, 201, disposed.data)
        self.assertEqual(disposed.data["proceeds"], Decimal("0"))
        self.assertEqual(disposed.data["loss"], Decimal("115000.0000"))
        self.assertIsNone(disposed.data["treasury_transaction"])
        self.assertEqual(TreasuryTransaction.objects.count(), 1)
        asset = __import__("assets.models", fromlist=["FixedAsset"]).FixedAsset.objects.get(pk=asset_id)
        self.assertEqual(asset.status, asset.Status.DISPOSED)


    def test_disposal_rejects_wrong_nature_gain_loss_account_without_partial_commit(self):
        created = self.client.post("/api/assets/fixed-assets/", self._payload(), format="json")
        self.assertEqual(created.status_code, 201, created.data)
        from assets.models import FixedAsset
        asset = FixedAsset.objects.get(pk=created.data["id"])
        asset.disposal_gain_loss_account = self.expense_gl
        asset.save(update_fields=("disposal_gain_loss_account",))
        before = JournalEntry.objects.count()
        failed = self.client.post(f"/api/assets/fixed-assets/{asset.pk}/dispose/", {
            "disposal_date": "2026-02-28", "proceeds": "120000.0000",
            "treasury_account": self.bank.pk, "fiscal_year": self.year.pk,
        }, format="json")
        self.assertEqual(failed.status_code, 400, failed.data)
        asset.refresh_from_db()
        self.assertEqual(asset.status, FixedAsset.Status.ACTIVE)
        self.assertEqual(JournalEntry.objects.count(), before)
        self.assertFalse(asset.depreciations.exists())

    def test_bulk_depreciation_run_posts_each_active_asset_once(self):
        created = self.client.post("/api/assets/fixed-assets/", self._payload(), format="json")
        self.assertEqual(created.status_code, 201, created.data)
        url = f"/api/assets/fixed-assets/depreciation-run/?company={self.company.pk}"
        payload = {"period_date": "2026-02-28", "fiscal_year": self.year.pk}
        first = self.client.post(url, payload, format="json")
        self.assertEqual(first.status_code, 200, first.data)
        self.assertEqual(first.data["processed_count"], 1)
        self.assertEqual(first.data["processed_total"], "5000.0000")
        second = self.client.post(url, payload, format="json")
        self.assertEqual(second.status_code, 200, second.data)
        self.assertEqual(second.data["processed_count"], 0)
        self.assertEqual(second.data["skipped_count"], 1)
        self.assertEqual(JournalEntry.objects.count(), 2)

    def test_asset_account_reconciliation_matches_asset_register_and_general_ledger(self):
        created = self.client.post("/api/assets/fixed-assets/", self._payload(), format="json")
        self.assertEqual(created.status_code, 201, created.data)
        asset_id = created.data["id"]
        depreciation = self.client.post(f"/api/assets/fixed-assets/{asset_id}/depreciate/", {
            "period_date": "2026-02-28", "fiscal_year": self.year.pk,
        }, format="json")
        self.assertEqual(depreciation.status_code, 201, depreciation.data)
        response = self.client.get(
            f"/api/assets/fixed-assets/account-reconciliation/?company={self.company.pk}"
            "&as_of=2026-02-28&date_from=2026-01-01&date_to=2026-02-28"
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["is_reconciled"], response.data)
        self.assertEqual(response.data["difference_count"], 0)
        self.assertEqual(response.data["cost_accounts"][0]["expected"], "120000.0000")
        self.assertEqual(response.data["accumulated_depreciation_accounts"][0]["expected"], "5000.0000")
        self.assertEqual(response.data["depreciation_expense_accounts"][0]["expected"], "5000.0000")

    def test_nonrecoverable_acquisition_tax_is_capitalized_into_asset_and_depreciation_basis(self):
        code = TaxCode.objects.create(
            company=self.company, country="EG", code="ASSET-NR-14",
            name_ar="ضريبة مدخلات غير قابلة للخصم", category=TaxCode.Category.STANDARD,
            rate=Decimal("14"), effective_from=date(2026, 1, 1),
            input_tax_recoverable=False,
        )
        payload = self._payload()
        payload.update({
            "code": "FA-TAX-001",
            "original_cost": "114000.0000",
            "acquisition_net_amount": "100000.0000",
            "nonrecoverable_tax_amount": "14000.0000",
            "acquisition_tax_code": code.pk,
        })
        created = self.client.post("/api/assets/fixed-assets/", payload, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        self.assertEqual(created.data["original_cost"], "114000.0000")
        self.assertEqual(created.data["nonrecoverable_tax_amount"], "14000.0000")
        acquisition = JournalEntry.objects.get(source_type=JournalEntry.Source.ASSETS)
        self.assertEqual(acquisition.lines.get(account=self.asset_gl).debit, Decimal("114000.0000"))
        transaction = TreasuryTransaction.objects.get(pk=created.data["acquisition_treasury_transaction"])
        self.assertEqual(transaction.amount, Decimal("114000.0000"))
        depreciation = self.client.post(
            f"/api/assets/fixed-assets/{created.data['id']}/depreciate/",
            {"period_date": "2026-02-28", "fiscal_year": self.year.pk},
            format="json",
        )
        self.assertEqual(depreciation.status_code, 201, depreciation.data)
        self.assertEqual(depreciation.data["depreciation_amount"], Decimal("4722.2222"))

    def test_asset_acquisition_rejects_recoverable_tax_as_nonrecoverable_cost(self):
        code = TaxCode.objects.create(
            company=self.company, country="EG", code="ASSET-REC-14",
            name_ar="ضريبة مدخلات قابلة للخصم", category=TaxCode.Category.STANDARD,
            rate=Decimal("14"), effective_from=date(2026, 1, 1),
            input_tax_recoverable=True,
        )
        payload = self._payload()
        payload.update({
            "original_cost": "114000.0000",
            "acquisition_net_amount": "100000.0000",
            "nonrecoverable_tax_amount": "14000.0000",
            "acquisition_tax_code": code.pk,
        })
        rejected = self.client.post("/api/assets/fixed-assets/", payload, format="json")
        self.assertEqual(rejected.status_code, 400, rejected.data)
        self.assertFalse(FixedAsset.objects.filter(code=payload["code"]).exists())

    def test_asset_accounts_must_separate_cost_accumulated_and_valid_acquisition_counter(self):
        from assets.models import FixedAsset
        same_contra = self._payload()
        same_contra["accumulated_depreciation_account"] = self.asset_gl.pk
        rejected_same = self.client.post("/api/assets/fixed-assets/", same_contra, format="json")
        self.assertEqual(rejected_same.status_code, 400, rejected_same.data)
        self.assertFalse(FixedAsset.objects.filter(code="FA-001").exists())

        expense_counter = self._payload()
        expense_counter["acquisition_counter_account"] = self.expense_gl.pk
        rejected_expense = self.client.post("/api/assets/fixed-assets/", expense_counter, format="json")
        self.assertEqual(rejected_expense.status_code, 400, rejected_expense.data)
        self.assertFalse(FixedAsset.objects.filter(code="FA-001").exists())
    def test_asset_classification_and_custody_details_are_saved_and_returned(self):
        payload = self._payload()
        payload.update({
            "category": "vehicle",
            "location": "المقر الرئيسي",
            "custodian": "مدير الحركة",
            "serial_number": "VIN-TEST-001",
            "warranty_end_date": "2027-01-10",
            "supplier_name": "مورد السيارات",
            "supplier_invoice": "INV-2026-77",
        })
        created = self.client.post("/api/assets/fixed-assets/", payload, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        for field, expected in (
            ("category", "vehicle"),
            ("location", "المقر الرئيسي"),
            ("custodian", "مدير الحركة"),
            ("serial_number", "VIN-TEST-001"),
            ("warranty_end_date", "2027-01-10"),
            ("supplier_name", "مورد السيارات"),
            ("supplier_invoice", "INV-2026-77"),
        ):
            self.assertEqual(created.data[field], expected)
        asset = FixedAsset.objects.get(pk=created.data["id"])
        self.assertEqual(asset.category, FixedAsset.Category.VEHICLE)
        self.assertEqual(asset.custodian, "مدير الحركة")

