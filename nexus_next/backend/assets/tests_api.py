from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.core.exceptions import ValidationError

from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from assets.models import AssetImpairment, AssetPurchaseInvoice, AssetSupplierPayment, FixedAsset
from assets.services import post_asset_supplier_payment
from accounting.models import AccountRole, ChartOfAccount, JournalEntry, TaxCode
from approvals.models import DiscountApprovalPolicy, FinancialApprovalRequest
from companies.models import Branch, Company, CompanyMembership, Currency, FiscalYear
from treasury.models import TreasuryAccount, TreasuryTransaction
from suppliers.models import Supplier


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

    def test_approved_cash_asset_acquisition_posts_recoverable_vat_adjustment(self):
        vat_input = self._account("1180", "ضريبة مدخلات قابلة للاسترداد", "asset")
        AccountRole.objects.create(company=self.company, role=AccountRole.Role.VAT_INPUT, account=vat_input)
        tax = TaxCode.objects.create(
            company=self.company, country="EG", code="VAT14-CASH-APP",
            name_ar="ضريبة أصل نقدي قابلة للاسترداد", category=TaxCode.Category.STANDARD,
            rate=Decimal("14"), effective_from=date(2026, 1, 1),
            input_tax_recoverable=True,
        )
        DiscountApprovalPolicy.objects.create(
            company=self.company, max_payment_amount=Decimal("50000"),
            created_by=self.user, updated_by=self.user,
        )
        payload = self._payload()
        payload.update({
            "original_cost": "100000.0000", "acquisition_net_amount": "100000.0000",
            "recoverable_tax_amount": "14000.0000", "acquisition_tax_code": tax.pk,
        })
        created = self.client.post("/api/assets/fixed-assets/", payload, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        asset_id = created.data["id"]
        self.assertIsNone(created.data["acquisition_journal"])
        treasury_doc = TreasuryTransaction.objects.get(pk=created.data["acquisition_treasury_transaction"])
        self.assertEqual(treasury_doc.amount, Decimal("114000.0000"))
        approval = FinancialApprovalRequest.objects.get(treasury_transaction=treasury_doc)
        manager = get_user_model().objects.create_user(username="asset-vat-manager", password="test-password")
        CompanyMembership.objects.create(user=manager, company=self.company, role="admin", all_branches=True)
        self.client.force_authenticate(manager)
        approved = self.client.post(f"/api/approvals/financial-requests/{approval.pk}/approve/", {}, format="json")
        self.assertEqual(approved.status_code, 200, approved.data)
        posted = self.client.post(f"/api/treasury/transactions/{treasury_doc.pk}/post/", {}, format="json")
        self.assertEqual(posted.status_code, 200, posted.data)
        asset = FixedAsset.objects.get(pk=asset_id)
        self.assertIsNotNone(asset.acquisition_journal_id)
        self.assertEqual(asset.acquisition_journal.lines.get(account=self.asset_gl).debit, Decimal("100000.0000"))
        self.assertEqual(asset.acquisition_journal.lines.get(account=vat_input).debit, Decimal("14000.0000"))
        self.assertEqual(asset.acquisition_journal.lines.get(account=self.cash_gl).credit, Decimal("114000.0000"))
        self.assertEqual(asset.acquisition_journal.lines.count(), 3)

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
        self.assertEqual(first.data["processed_total"], "5000.00")
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
        self.assertEqual(depreciation.data["depreciation_amount"], Decimal("4722.22"))

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



    def test_cash_asset_purchase_recoverable_vat_is_investing_and_balances_all_statements(self):
        vat_input = self._account("1180", "ضريبة مدخلات قابلة للاسترداد", "asset")
        AccountRole.objects.create(company=self.company, role=AccountRole.Role.VAT_INPUT, account=vat_input)
        tax = TaxCode.objects.create(
            company=self.company, country="EG", code="VAT14-CASH-FA",
            name_ar="ضريبة شراء أصل قابلة للاسترداد", category=TaxCode.Category.STANDARD,
            rate=Decimal("14"), effective_from=date(2026, 1, 1),
            input_tax_recoverable=True,
        )
        payload = self._payload()
        payload.update({
            "original_cost": "100000.0000",
            "acquisition_net_amount": "100000.0000",
            "recoverable_tax_amount": "14000.0000",
            "nonrecoverable_tax_amount": "0.0000",
            "acquisition_tax_code": tax.pk,
        })
        created = self.client.post("/api/assets/fixed-assets/", payload, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        asset = FixedAsset.objects.get(pk=created.data["id"])
        self.assertEqual(asset.original_cost, Decimal("100000.0000"))
        self.assertEqual(asset.recoverable_tax_amount, Decimal("14000.0000"))
        cash_document = TreasuryTransaction.objects.get(pk=asset.acquisition_treasury_transaction_id)
        self.assertEqual(cash_document.amount, Decimal("114000.0000"))
        self.assertEqual(cash_document.cash_flow_category, TreasuryTransaction.CashFlowCategory.INVESTING)
        self.assertEqual(asset.acquisition_journal.lines.get(account=self.asset_gl).debit, Decimal("100000.0000"))
        self.assertEqual(asset.acquisition_journal.lines.get(account=vat_input).debit, Decimal("14000.0000"))
        self.assertEqual(asset.acquisition_journal.lines.get(account=self.cash_gl).credit, Decimal("114000.0000"))
        self.assertEqual(asset.acquisition_journal.lines.count(), 3)
        totals = asset.acquisition_journal.lines.aggregate(
            debit=__import__("django.db.models", fromlist=["Sum"]).Sum("debit"),
            credit=__import__("django.db.models", fromlist=["Sum"]).Sum("credit"),
        )
        self.assertEqual(totals["debit"], totals["credit"])

        income = self.client.get("/api/accounting/journal-entries/income-statement/", {
            "company": self.company.pk, "date_from": "2026-01-01", "date_to": "2026-01-31",
        })
        self.assertEqual(income.status_code, 200, income.data)
        self.assertEqual(income.data["net_profit"], Decimal("0.0000"))

        balance = self.client.get("/api/accounting/journal-entries/balance-sheet/", {
            "company": self.company.pk, "as_of": "2026-01-10",
        })
        self.assertEqual(balance.status_code, 200, balance.data)
        self.assertEqual(balance.data["difference"], Decimal("0.0000"))
        self.assertEqual(next(row["balance"] for row in balance.data["assets"] if row["account_id"] == self.asset_gl.pk), Decimal("100000.0000"))
        self.assertEqual(next(row["balance"] for row in balance.data["assets"] if row["account_id"] == vat_input.pk), Decimal("14000.0000"))

        cash_flow = self.client.get("/api/accounting/journal-entries/indirect-cash-flow/", {
            "company": self.company.pk, "date_from": "2026-01-01", "date_to": "2026-01-31",
        })
        self.assertEqual(cash_flow.status_code, 200, cash_flow.data)
        self.assertEqual(cash_flow.data["operating_cash_flow"], Decimal("0.0000"))
        self.assertEqual(cash_flow.data["investing_cash_flow"], Decimal("-114000.0000"))
        self.assertEqual(cash_flow.data["net_change_in_cash"], Decimal("-114000.0000"))
        self.assertEqual(cash_flow.data["reconciliation_difference"], Decimal("0.0000"))

    def test_asset_impairment_updates_income_balance_sheet_and_indirect_cash_flow(self):
        created = self.client.post("/api/assets/fixed-assets/", self._payload(), format="json")
        self.assertEqual(created.status_code, 201, created.data)
        asset_id = created.data["id"]
        depreciation = self.client.post(f"/api/assets/fixed-assets/{asset_id}/depreciate/", {
            "period_date": "2026-02-28", "fiscal_year": self.year.pk,
        }, format="json")
        self.assertEqual(depreciation.status_code, 201, depreciation.data)
        impairment_expense = self._account("6200", "خسائر انخفاض قيمة الأصول", "expense")
        accumulated_impairment = self._account("1595", "مجمع انخفاض قيمة الأصول", "asset")
        response = self.client.post(f"/api/assets/fixed-assets/{asset_id}/impair/", {
            "impairment_date": "2026-02-28", "recoverable_amount": "90000.0000",
            "impairment_expense_account": impairment_expense.pk,
            "accumulated_impairment_account": accumulated_impairment.pk,
            "fiscal_year": self.year.pk,
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        impairment = AssetImpairment.objects.get(pk=response.data["id"])
        self.assertEqual(impairment.carrying_amount_before, Decimal("115000.0000"))
        self.assertEqual(impairment.impairment_loss, Decimal("25000.0000"))
        self.assertEqual(impairment.journal_entry.lines.get(account=impairment_expense).debit, Decimal("25000.0000"))
        self.assertEqual(impairment.journal_entry.lines.get(account=accumulated_impairment).credit, Decimal("25000.0000"))
        self.assertEqual(response.data["oci_impact"], "none_under_cost_model")

        schedule = self.client.get(f"/api/assets/fixed-assets/{asset_id}/schedule/")
        self.assertEqual(schedule.status_code, 200, schedule.data)
        self.assertEqual(schedule.data["accumulated_impairment"], Decimal("25000.0000"))
        self.assertEqual(schedule.data["net_book_value"], Decimal("90000.0000"))
        register = self.client.get("/api/assets/fixed-assets/register/", {
            "company": self.company.pk, "as_of": "2026-02-28",
        })
        self.assertEqual(register.status_code, 200, register.data)
        self.assertEqual(register.data["rows"][0]["accumulated_impairment"], Decimal("25000.0000"))
        self.assertEqual(register.data["rows"][0]["net_book_value"], Decimal("90000.0000"))
        movements = self.client.get("/api/assets/fixed-assets/movement-report/", {
            "company": self.company.pk, "date_from": "2026-01-01", "date_to": "2026-02-28",
        })
        self.assertEqual(movements.status_code, 200, movements.data)
        impairment_event = next(row for row in movements.data["rows"] if row["event_type"] == "impairment")
        self.assertEqual(impairment_event["amount"], Decimal("25000.0000"))

        income = self.client.get("/api/accounting/journal-entries/income-statement/", {
            "company": self.company.pk, "date_from": "2026-01-01", "date_to": "2026-02-28",
        })
        self.assertEqual(income.status_code, 200, income.data)
        self.assertEqual(income.data["expenses_total"], Decimal("30000.0000"))
        self.assertEqual(income.data["net_profit"], Decimal("-30000.0000"))

        balance = self.client.get("/api/accounting/journal-entries/balance-sheet/", {
            "company": self.company.pk, "as_of": "2026-02-28",
        })
        self.assertEqual(balance.status_code, 200, balance.data)
        self.assertEqual(balance.data["difference"], Decimal("0.0000"))
        self.assertEqual(balance.data["current_year_profit"], Decimal("-30000.0000"))
        self.assertEqual(next(row["balance"] for row in balance.data["assets"] if row["account_id"] == accumulated_impairment.pk), Decimal("-25000.0000"))

        cash_flow = self.client.get("/api/accounting/journal-entries/indirect-cash-flow/", {
            "company": self.company.pk, "date_from": "2026-01-01", "date_to": "2026-02-28",
        })
        self.assertEqual(cash_flow.status_code, 200, cash_flow.data)
        self.assertEqual(cash_flow.data["net_profit"], Decimal("-30000.0000"))
        self.assertEqual(cash_flow.data["non_cash_adjustments"]["depreciation"], Decimal("5000.0000"))
        self.assertEqual(cash_flow.data["non_cash_adjustments"]["impairment_loss"], Decimal("25000.0000"))
        self.assertEqual(cash_flow.data["operating_cash_flow"], Decimal("0.0000"))
        self.assertEqual(cash_flow.data["investing_cash_flow"], Decimal("-120000.0000"))
        self.assertEqual(cash_flow.data["reconciliation_difference"], Decimal("0.0000"))

        equity = self.client.get("/api/accounting/journal-entries/changes-in-equity/", {
            "company": self.company.pk, "date_from": "2026-01-01", "date_to": "2026-02-28",
        })
        self.assertEqual(equity.status_code, 200, equity.data)
        self.assertEqual(equity.data["period_net_profit"], Decimal("-30000.0000"))
        self.assertEqual(equity.data["closing_equity"], Decimal("-30000.0000"))

        direct_cash_flow = self.client.get("/api/accounting/journal-entries/direct-cash-flow/", {
            "company": self.company.pk, "date_from": "2026-01-01", "date_to": "2026-02-28",
        })
        self.assertEqual(direct_cash_flow.status_code, 200, direct_cash_flow.data)
        self.assertEqual(direct_cash_flow.data["investing_cash_flow"], Decimal("-120000.0000"))
        self.assertEqual(direct_cash_flow.data["opening_cash_balance"], Decimal("0.0000"))
        self.assertEqual(direct_cash_flow.data["net_change_in_cash"], Decimal("-120000.0000"))
        self.assertEqual(direct_cash_flow.data["closing_cash_balance"], Decimal("-120000.0000"))
        self.assertEqual(direct_cash_flow.data["ledger_cash_change"], Decimal("-120000.0000"))
        self.assertEqual(direct_cash_flow.data["ledger_closing_cash_balance"], Decimal("-120000.0000"))
        self.assertEqual(direct_cash_flow.data["closing_balance_difference"], Decimal("0.0000"))
        self.assertEqual(direct_cash_flow.data["reconciliation_difference"], Decimal("0.0000"))

        reconciliation = self.client.get("/api/assets/fixed-assets/account-reconciliation/", {
            "company": self.company.pk, "as_of": "2026-02-28",
            "date_from": "2026-01-01", "date_to": "2026-02-28",
        })
        self.assertEqual(reconciliation.status_code, 200, reconciliation.data)
        self.assertTrue(reconciliation.data["is_reconciled"], reconciliation.data)

        next_depreciation = self.client.post(f"/api/assets/fixed-assets/{asset_id}/depreciate/", {
            "period_date": "2026-03-31", "fiscal_year": self.year.pk,
        }, format="json")
        self.assertEqual(next_depreciation.status_code, 201, next_depreciation.data)
        self.assertGreater(next_depreciation.data["depreciation_amount"], Decimal("0"))
        self.assertLess(next_depreciation.data["depreciation_amount"], Decimal("5000.0000"))

    def test_direct_asset_vendor_invoice_posts_to_supplier_subledger_and_bank_payment(self):
        payable = self._account("2100", "الموردون", "liability")
        vat_input = self._account("1180", "ضريبة مدخلات", "asset")
        AccountRole.objects.create(company=self.company, role=AccountRole.Role.ACCOUNTS_PAYABLE, account=payable)
        AccountRole.objects.create(company=self.company, role=AccountRole.Role.VAT_INPUT, account=vat_input)
        supplier = Supplier.objects.create(company=self.company, code="SUP-FA", name_ar="مورد أصول", payment_terms_days=30)
        tax = TaxCode.objects.create(
            company=self.company, country="EG", code="VAT14-FA",
            name_ar="ضريبة مدخلات 14%", category=TaxCode.Category.STANDARD,
            rate=Decimal("14"), effective_from=date(2026, 1, 1),
            input_tax_recoverable=True,
        )
        payload = self._payload()
        payload["acquisition_mode"] = "supplier_invoice"
        created = self.client.post("/api/assets/fixed-assets/", payload, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        asset_id = created.data["id"]
        self.assertIsNone(created.data["acquisition_journal"])
        self.assertEqual(JournalEntry.objects.count(), 0)

        invoice_response = self.client.post(
            f"/api/assets/fixed-assets/{asset_id}/purchase-invoice/",
            {
                "supplier": supplier.pk, "invoice_number": "FA-SUP-1001",
                "invoice_date": "2026-01-10", "net_amount": "100000.0000",
                "tax_code": tax.pk, "fiscal_year": self.year.pk,
            },
            format="json",
        )
        self.assertEqual(invoice_response.status_code, 201, invoice_response.data)
        self.assertEqual(invoice_response.data["grand_total"], Decimal("114000.0000"))
        self.assertEqual(invoice_response.data["recoverable_tax_amount"], Decimal("14000.0000"))
        invoice = AssetPurchaseInvoice.objects.get(pk=invoice_response.data["id"])
        asset = FixedAsset.objects.get(pk=asset_id)
        self.assertEqual(asset.original_cost, Decimal("100000.0000"))
        self.assertEqual(asset.acquisition_journal_id, invoice.journal_entry_id)
        self.assertEqual(invoice.journal_entry.status, JournalEntry.Status.POSTED)
        self.assertEqual(invoice.journal_entry.lines.get(account=self.asset_gl).debit, Decimal("100000.0000"))
        self.assertEqual(invoice.journal_entry.lines.get(account=vat_input).debit, Decimal("14000.0000"))
        self.assertEqual(invoice.journal_entry.lines.get(account=payable).credit, Decimal("114000.0000"))

        payment_response = self.client.post(
            f"/api/assets/fixed-assets/{asset_id}/supplier-payment/",
            {
                "payment_number": "FA-PAY-1001", "payment_date": "2026-01-15",
                "payment_account": self.cash_gl.pk, "amount": "50000.0000",
                "reference": "BANK-REF-01",
            },
            format="json",
        )
        self.assertEqual(payment_response.status_code, 201, payment_response.data)
        payment = AssetSupplierPayment.objects.get(pk=payment_response.data["id"])
        self.assertEqual(payment.status, AssetSupplierPayment.Status.POSTED)
        self.assertEqual(payment.journal_entry.lines.get(account=payable).debit, Decimal("50000.0000"))
        self.assertEqual(payment.journal_entry.lines.get(account=self.cash_gl).credit, Decimal("50000.0000"))
        bank_movement = TreasuryTransaction.objects.get(
            document_number=f"AUTO-FA-PAY-{payment.pk}",
            journal_entry=payment.journal_entry, status=TreasuryTransaction.Status.POSTED,
        )
        self.assertEqual(bank_movement.cash_flow_category, TreasuryTransaction.CashFlowCategory.INVESTING)

        statement = self.client.get("/api/purchasing/invoices/supplier-statement/", {
            "company": self.company.pk, "supplier": supplier.pk,
        })
        self.assertEqual(statement.status_code, 200, statement.data)
        self.assertEqual(statement.data["closing_balance"], "64000.0000")
        self.assertEqual(
            {row["document_type"] for row in statement.data["rows"]},
            {"asset_invoice", "asset_payment"},
        )

        # The same asset payable must be visible in the shared supplier aging report.
        aging = self.client.get("/api/purchasing/invoices/supplier-aging/", {
            "company": self.company.pk, "as_of": "2026-01-20",
        })
        self.assertEqual(aging.status_code, 200, aging.data)
        self.assertEqual(aging.data["totals"]["total_due"], "64000.0000")
        self.assertEqual(aging.data["totals"]["not_due"], "64000.0000")
        asset_aging_line = aging.data["suppliers"][0]["invoices"][0]
        self.assertEqual(asset_aging_line["document_type"], "asset_purchase_invoice")
        self.assertEqual(asset_aging_line["asset_name"], "سيارة الشركة")

        # A second settlement must clear the exact remaining balance without
        # changing the original invoice or duplicating the first payment.
        final_payment_response = self.client.post(
            f"/api/assets/fixed-assets/{asset_id}/supplier-payment/",
            {
                "payment_number": "FA-PAY-1001-FINAL", "payment_date": "2026-01-20",
                "payment_account": self.cash_gl.pk, "amount": "64000.0000",
                "reference": "BANK-REF-02",
            },
            format="json",
        )
        self.assertEqual(final_payment_response.status_code, 201, final_payment_response.data)
        final_payment = AssetSupplierPayment.objects.get(pk=final_payment_response.data["id"])
        self.assertEqual(final_payment.status, AssetSupplierPayment.Status.POSTED)
        self.assertEqual(final_payment.journal_entry.lines.get(account=payable).debit, Decimal("64000.0000"))
        self.assertEqual(final_payment.journal_entry.lines.get(account=self.cash_gl).credit, Decimal("64000.0000"))
        self.assertEqual(
            AssetSupplierPayment.objects.filter(invoice=invoice, status=AssetSupplierPayment.Status.POSTED)
            .aggregate(total=__import__("django.db.models", fromlist=["Sum"]).Sum("amount"))["total"],
            Decimal("114000.0000"),
        )
        statement_after_full_settlement = self.client.get("/api/purchasing/invoices/supplier-statement/", {
            "company": self.company.pk, "supplier": supplier.pk,
        })
        self.assertEqual(statement_after_full_settlement.status_code, 200, statement_after_full_settlement.data)
        self.assertEqual(statement_after_full_settlement.data["closing_balance"], "0.0000")
        aging_after_full_settlement = self.client.get("/api/purchasing/invoices/supplier-aging/", {
            "company": self.company.pk, "as_of": "2026-01-20",
        })
        self.assertEqual(aging_after_full_settlement.status_code, 200, aging_after_full_settlement.data)
        self.assertEqual(Decimal(aging_after_full_settlement.data["totals"]["total_due"]), Decimal("0.0000"))

        partial_cash_flow = self.client.get("/api/accounting/journal-entries/indirect-cash-flow/", {
            "company": self.company.pk, "date_from": "2026-01-01", "date_to": "2026-01-15",
        })
        self.assertEqual(partial_cash_flow.status_code, 200, partial_cash_flow.data)
        self.assertEqual(partial_cash_flow.data["operating_cash_flow"], Decimal("0.0000"))
        self.assertEqual(partial_cash_flow.data["investing_cash_flow"], Decimal("-50000.0000"))
        self.assertEqual(partial_cash_flow.data["reconciliation_difference"], Decimal("0.0000"))
        self.assertEqual(partial_cash_flow.data["non_cash_investing_transactions"][0]["gross_amount"], Decimal("114000.0000"))
        self.assertEqual(partial_cash_flow.data["non_cash_investing_transactions"][0]["cash_paid_during_period"], Decimal("50000.0000"))
        self.assertEqual(partial_cash_flow.data["non_cash_investing_transactions"][0]["unpaid_at_period_end"], Decimal("64000.0000"))

        full_cash_flow = self.client.get("/api/accounting/journal-entries/indirect-cash-flow/", {
            "company": self.company.pk, "date_from": "2026-01-01", "date_to": "2026-01-20",
        })
        self.assertEqual(full_cash_flow.status_code, 200, full_cash_flow.data)
        self.assertEqual(full_cash_flow.data["operating_cash_flow"], Decimal("0.0000"))
        self.assertEqual(full_cash_flow.data["investing_cash_flow"], Decimal("-114000.0000"))
        self.assertEqual(full_cash_flow.data["net_change_in_cash"], Decimal("-114000.0000"))
        self.assertEqual(full_cash_flow.data["reconciliation_difference"], Decimal("0.0000"))
        self.assertEqual(full_cash_flow.data["non_cash_investing_transactions"][0]["unpaid_at_period_end"], Decimal("0.0000"))
        self.assertEqual(full_cash_flow.data["non_cash_investing_transactions"][0]["cash_paid_during_period"], Decimal("114000.0000"))

        overpayment = self.client.post(
            f"/api/assets/fixed-assets/{asset_id}/supplier-payment/",
            {
                "payment_number": "FA-PAY-1002", "payment_date": "2026-01-20",
                "payment_account": self.cash_gl.pk, "amount": "70000.0000",
            },
            format="json",
        )
        self.assertEqual(overpayment.status_code, 400, overpayment.data)
        self.assertFalse(AssetSupplierPayment.objects.filter(payment_number="FA-PAY-1002").exists())

    def _create_supplier_asset_invoice(self, amount="100000.0000"):
        payable = self._account("2100", "الموردون", "liability")
        AccountRole.objects.create(company=self.company, role=AccountRole.Role.ACCOUNTS_PAYABLE, account=payable)
        supplier = Supplier.objects.create(
            company=self.company, code="SUP-FA-TREASURY", name_ar="مورد اختبار سداد الأصول",
        )
        payload = self._payload()
        payload["acquisition_mode"] = "supplier_invoice"
        created = self.client.post("/api/assets/fixed-assets/", payload, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        invoice_response = self.client.post(
            f"/api/assets/fixed-assets/{created.data['id']}/purchase-invoice/",
            {
                "supplier": supplier.pk, "invoice_number": "FA-TREASURY-INV",
                "invoice_date": "2026-01-10", "net_amount": amount,
                "fiscal_year": self.year.pk,
            },
            format="json",
        )
        self.assertEqual(invoice_response.status_code, 201, invoice_response.data)
        invoice = AssetPurchaseInvoice.objects.get(pk=invoice_response.data["id"])
        self.assertEqual(invoice.status, AssetPurchaseInvoice.Status.POSTED)
        return invoice, supplier

    def _asset_supplier_payment(self, invoice, supplier, account, number, amount="100.0000"):
        return AssetSupplierPayment.objects.create(
            company=self.company, branch=self.branch, invoice=invoice, supplier=supplier,
            payment_number=number, payment_date=date(2026, 1, 15),
            payment_account=account, amount=Decimal(amount),
        )

    def test_asset_supplier_payment_accepts_cash_treasury_and_posts_investing_movement(self):
        invoice, supplier = self._create_supplier_asset_invoice()
        cash_gl = self._account("1101", "الصندوق", "asset")
        cash_treasury = TreasuryAccount.objects.create(
            company=self.company, branch=self.branch, account=cash_gl,
            kind=TreasuryAccount.Kind.CASH, code="CASH-FA-PAY",
            name_ar="صندوق سداد الأصول", opening_balance=Decimal("10000"),
        )
        payment = self._asset_supplier_payment(
            invoice, supplier, cash_gl, "FA-CASH-PAY-1", "2500.0000",
        )

        posted = post_asset_supplier_payment(payment.pk, self.user)

        self.assertEqual(posted.status, AssetSupplierPayment.Status.POSTED)
        self.assertEqual(posted.journal_entry.lines.get(account=cash_gl).credit, Decimal("2500.0000"))
        movement = TreasuryTransaction.objects.get(
            document_number=f"AUTO-FA-PAY-{payment.pk}",
            journal_entry=posted.journal_entry,
        )
        self.assertEqual(movement.source_account, cash_treasury)
        self.assertEqual(movement.kind, TreasuryTransaction.Kind.PAYMENT)
        self.assertEqual(movement.cash_flow_category, TreasuryTransaction.CashFlowCategory.INVESTING)

    def test_asset_supplier_payment_rejects_insufficient_treasury_balance_before_journal(self):
        invoice, supplier = self._create_supplier_asset_invoice()
        low_gl = self._account("1102", "صندوق منخفض الرصيد", "asset")
        TreasuryAccount.objects.create(
            company=self.company, branch=self.branch, account=low_gl,
            kind=TreasuryAccount.Kind.CASH, code="CASH-FA-LOW",
            name_ar="صندوق منخفض الرصيد", opening_balance=Decimal("50"),
        )
        payment = self._asset_supplier_payment(
            invoice, supplier, low_gl, "FA-LOW-PAY-1", "100.0000",
        )
        journal_count = JournalEntry.objects.count()

        with self.assertRaises(ValidationError):
            post_asset_supplier_payment(payment.pk, self.user)

        payment.refresh_from_db()
        self.assertEqual(payment.status, AssetSupplierPayment.Status.DRAFT)
        self.assertIsNone(payment.journal_entry_id)
        self.assertEqual(JournalEntry.objects.count(), journal_count)
        self.assertFalse(TreasuryTransaction.objects.filter(
            document_number=f"AUTO-FA-PAY-{payment.pk}",
        ).exists())

    def _post_cash_gl_movement(self, account, number, movement_date, debit, credit):
        from accounting.services import post_journal

        offset = self.gain_loss_gl if Decimal(debit) > 0 else self.expense_gl
        entry = JournalEntry.objects.create(
            company=self.company, branch=self.branch, fiscal_year=self.year,
            entry_number=number, entry_date=movement_date,
            description="حركة اختبار على حساب الخزينة",
        )
        JournalLine.objects.create(
            entry=entry, account=account, debit=Decimal(debit), credit=Decimal(credit),
        )
        JournalLine.objects.create(
            entry=entry, account=offset, debit=Decimal(credit), credit=Decimal(debit),
        )
        return post_journal(entry.pk, actor=self.user)

    def test_asset_supplier_payment_does_not_use_deposits_after_payment_date(self):
        invoice, supplier = self._create_supplier_asset_invoice()
        cash_gl = self._account("1105", "خزينة بإيداع لاحق", "asset")
        TreasuryAccount.objects.create(
            company=self.company, branch=self.branch, account=cash_gl,
            kind=TreasuryAccount.Kind.CASH, code="CASH-FA-FUTURE-DEPOSIT",
            name_ar="خزينة بإيداع لاحق", opening_balance=Decimal("0"),
        )
        self._post_cash_gl_movement(
            cash_gl, "FA-FUTURE-DEPOSIT", date(2026, 1, 20), "500", "0",
        )
        payment = self._asset_supplier_payment(
            invoice, supplier, cash_gl, "FA-BACKDATED-NO-FUND", "100.0000",
        )
        journal_count = JournalEntry.objects.count()

        with self.assertRaises(ValidationError):
            post_asset_supplier_payment(payment.pk, self.user)

        payment.refresh_from_db()
        self.assertEqual(payment.status, AssetSupplierPayment.Status.DRAFT)
        self.assertIsNone(payment.journal_entry_id)
        self.assertEqual(JournalEntry.objects.count(), journal_count)

    def test_asset_supplier_payment_rejects_intermediate_future_overdraft(self):
        invoice, supplier = self._create_supplier_asset_invoice()
        cash_gl = self._account("1106", "خزينة بحركة مستقبلية", "asset")
        TreasuryAccount.objects.create(
            company=self.company, branch=self.branch, account=cash_gl,
            kind=TreasuryAccount.Kind.CASH, code="CASH-FA-FUTURE-WITHDRAWAL",
            name_ar="خزينة بحركة مستقبلية", opening_balance=Decimal("200"),
        )
        self._post_cash_gl_movement(
            cash_gl, "FA-FUTURE-WITHDRAWAL", date(2026, 1, 20), "0", "180",
        )
        self._post_cash_gl_movement(
            cash_gl, "FA-FUTURE-DEPOSIT-2", date(2026, 1, 25), "100", "0",
        )
        payment = self._asset_supplier_payment(
            invoice, supplier, cash_gl, "FA-BACKDATED-OVERDRAFT", "100.0000",
        )
        journal_count = JournalEntry.objects.count()

        with self.assertRaises(ValidationError):
            post_asset_supplier_payment(payment.pk, self.user)

        payment.refresh_from_db()
        self.assertEqual(payment.status, AssetSupplierPayment.Status.DRAFT)
        self.assertIsNone(payment.journal_entry_id)
        self.assertEqual(JournalEntry.objects.count(), journal_count)

    def test_asset_supplier_payment_rejects_unlinked_inactive_wrong_branch_and_foreign_treasury(self):
        invoice, supplier = self._create_supplier_asset_invoice()
        other_branch = Branch.objects.create(
            company=self.company, code="OTHER", name_ar="فرع آخر",
        )
        invalid_accounts = []

        unlinked_gl = self._account("1111", "حساب غير مربوط بخزينة", "asset")
        invalid_accounts.append(("FA-UNLINKED", unlinked_gl))

        inactive_gl = self._account("1112", "خزينة موقوفة", "asset")
        TreasuryAccount.objects.create(
            company=self.company, branch=self.branch, account=inactive_gl,
            kind=TreasuryAccount.Kind.BANK, code="BANK-FA-INACTIVE",
            name_ar="بنك موقوف", is_active=False,
        )
        invalid_accounts.append(("FA-INACTIVE", inactive_gl))

        wrong_branch_gl = self._account("1113", "خزينة فرع آخر", "asset")
        TreasuryAccount.objects.create(
            company=self.company, branch=other_branch, account=wrong_branch_gl,
            kind=TreasuryAccount.Kind.CASH, code="CASH-FA-OTHER",
            name_ar="خزينة فرع آخر",
        )
        invalid_accounts.append(("FA-WRONG-BRANCH", wrong_branch_gl))

        foreign_gl = self._account("1114", "بنك بعملة أجنبية", "asset")
        TreasuryAccount.objects.create(
            company=self.company, branch=self.branch, account=foreign_gl,
            kind=TreasuryAccount.Kind.BANK, code="BANK-FA-USD",
            name_ar="بنك دولار", currency_code="USD",
        )
        invalid_accounts.append(("FA-FOREIGN", foreign_gl))

        journal_count = JournalEntry.objects.count()
        for number, account in invalid_accounts:
            with self.subTest(payment_number=number):
                payment = self._asset_supplier_payment(
                    invoice, supplier, account, number, "100.0000",
                )
                with self.assertRaises(ValidationError):
                    post_asset_supplier_payment(payment.pk, self.user)
                payment.refresh_from_db()
                self.assertEqual(payment.status, AssetSupplierPayment.Status.DRAFT)
                self.assertIsNone(payment.journal_entry_id)
        self.assertEqual(JournalEntry.objects.count(), journal_count)

    def test_asset_supplier_payment_rolls_back_journal_if_treasury_mirror_fails(self):
        invoice, supplier = self._create_supplier_asset_invoice()
        payment = self._asset_supplier_payment(
            invoice, supplier, self.cash_gl, "FA-ROLLBACK-PAY-1", "100.0000",
        )
        journal_count = JournalEntry.objects.count()

        with patch(
            "treasury.services.record_posted_document_bank_movement",
            side_effect=ValidationError("treasury mirror failure"),
        ):
            with self.assertRaises(ValidationError):
                post_asset_supplier_payment(payment.pk, self.user)

        payment.refresh_from_db()
        self.assertEqual(payment.status, AssetSupplierPayment.Status.DRAFT)
        self.assertIsNone(payment.journal_entry_id)
        self.assertEqual(JournalEntry.objects.count(), journal_count)
        self.assertFalse(TreasuryTransaction.objects.filter(
            document_number=f"AUTO-FA-PAY-{payment.pk}",
        ).exists())

    def test_asset_supplier_payment_cannot_be_posted_twice(self):
        invoice, supplier = self._create_supplier_asset_invoice()
        payment = self._asset_supplier_payment(
            invoice, supplier, self.cash_gl, "FA-DUPLICATE-PAY-1", "100.0000",
        )

        posted = post_asset_supplier_payment(payment.pk, self.user)
        with self.assertRaises(ValidationError):
            post_asset_supplier_payment(payment.pk, self.user)

        self.assertEqual(
            TreasuryTransaction.objects.filter(
                document_number=f"AUTO-FA-PAY-{payment.pk}",
                journal_entry=posted.journal_entry,
            ).count(),
            1,
        )

    def test_asset_vendor_invoice_requires_financial_approval_above_policy_limit(self):
        payable = self._account("2100", "الموردون", "liability")
        AccountRole.objects.create(company=self.company, role=AccountRole.Role.ACCOUNTS_PAYABLE, account=payable)
        supplier = Supplier.objects.create(company=self.company, code="SUP-FA-APP", name_ar="مورد اعتماد")
        DiscountApprovalPolicy.objects.create(
            company=self.company, max_purchase_invoice_amount=Decimal("50000"),
            created_by=self.user, updated_by=self.user,
        )
        payload = self._payload()
        payload["acquisition_mode"] = "supplier_invoice"
        created = self.client.post("/api/assets/fixed-assets/", payload, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        asset_id = created.data["id"]
        invoice_response = self.client.post(
            f"/api/assets/fixed-assets/{asset_id}/purchase-invoice/",
            {
                "supplier": supplier.pk, "invoice_number": "FA-SUP-APP-1",
                "invoice_date": "2026-01-10", "net_amount": "120000.0000",
                "fiscal_year": self.year.pk,
            },
            format="json",
        )
        self.assertEqual(invoice_response.status_code, 201, invoice_response.data)
        self.assertTrue(invoice_response.data["approval_required"])
        invoice = AssetPurchaseInvoice.objects.get(pk=invoice_response.data["id"])
        self.assertEqual(invoice.status, AssetPurchaseInvoice.Status.DRAFT)
        self.assertIsNone(invoice.journal_entry_id)
        approval = FinancialApprovalRequest.objects.get(
            document_type=FinancialApprovalRequest.DocumentType.ASSET_PURCHASE_INVOICE,
            asset_purchase_invoice=invoice,
        )
        manager = get_user_model().objects.create_user(username="asset-invoice-manager", password="test-password")
        CompanyMembership.objects.create(user=manager, company=self.company, role="admin", all_branches=True)
        self.client.force_authenticate(manager)
        approved = self.client.post(f"/api/approvals/financial-requests/{approval.pk}/approve/", {}, format="json")
        self.assertEqual(approved.status_code, 200, approved.data)
        posted = self.client.post(f"/api/assets/fixed-assets/{asset_id}/post-purchase-invoice/", {}, format="json")
        self.assertEqual(posted.status_code, 200, posted.data)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, AssetPurchaseInvoice.Status.POSTED)
        self.assertIsNotNone(invoice.journal_entry_id)


    def test_nonrecoverable_input_tax_is_capitalized_into_fixed_asset_cost(self):
        payable = self._account("2100", "الموردون", "liability")
        AccountRole.objects.create(company=self.company, role=AccountRole.Role.ACCOUNTS_PAYABLE, account=payable)
        supplier = Supplier.objects.create(company=self.company, code="SUP-FA-NR", name_ar="مورد ضريبة غير مستردة")
        tax = TaxCode.objects.create(
            company=self.company, country="EG", code="VAT14-FA-NR",
            name_ar="ضريبة غير قابلة للاسترداد", category=TaxCode.Category.STANDARD,
            rate=Decimal("14"), effective_from=date(2026, 1, 1),
            input_tax_recoverable=False,
        )
        payload = self._payload()
        payload["acquisition_mode"] = "supplier_invoice"
        created = self.client.post("/api/assets/fixed-assets/", payload, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        response = self.client.post(
            f"/api/assets/fixed-assets/{created.data['id']}/purchase-invoice/",
            {
                "supplier": supplier.pk, "invoice_number": "FA-SUP-NR-1",
                "invoice_date": "2026-01-10", "net_amount": "100000.0000",
                "tax_code": tax.pk, "fiscal_year": self.year.pk,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        asset = FixedAsset.objects.get(pk=created.data["id"])
        invoice = AssetPurchaseInvoice.objects.get(pk=response.data["id"])
        self.assertEqual(invoice.nonrecoverable_tax_amount, Decimal("14000.0000"))
        self.assertEqual(invoice.recoverable_tax_amount, Decimal("0.0000"))
        self.assertEqual(asset.original_cost, Decimal("114000.0000"))
        self.assertEqual(invoice.journal_entry.lines.get(account=self.asset_gl).debit, Decimal("114000.0000"))
        self.assertEqual(invoice.journal_entry.lines.get(account=payable).credit, Decimal("114000.0000"))
        self.assertEqual(invoice.journal_entry.lines.count(), 2)

    def test_asset_invoice_and_payment_errors_hide_internal_exception_details(self):
        payload = self._payload()
        payload["acquisition_mode"] = "supplier_invoice"
        created = self.client.post("/api/assets/fixed-assets/", payload, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        asset_id = created.data["id"]

        invoice_error = self.client.post(
            f"/api/assets/fixed-assets/{asset_id}/purchase-invoice/",
            {
                "supplier": 999999, "invoice_number": "FA-SUP-INVALID",
                "invoice_date": "2026-01-10", "net_amount": "1000.0000",
                "fiscal_year": self.year.pk,
            },
            format="json",
        )
        self.assertEqual(invoice_error.status_code, 400, invoice_error.data)
        self.assertIn(
            "تعذر معالجة فاتورة شراء الأصل. راجع البيانات والمراجع المرتبطة ثم أعد المحاولة.",
            str(invoice_error.data),
        )
        self.assertNotIn("DoesNotExist", str(invoice_error.data))

        payment_error = self.client.post(
            f"/api/assets/fixed-assets/{asset_id}/supplier-payment/",
            {
                "payment_number": "FA-PAY-INVALID", "payment_date": "2026-01-15",
                "payment_account": self.cash_gl.pk, "amount": "100.0000",
            },
            format="json",
        )
        self.assertEqual(payment_error.status_code, 400, payment_error.data)
        self.assertIn(
            "تعذر معالجة سند سداد الأصل. راجع البيانات والمراجع المرتبطة ثم أعد المحاولة.",
            str(payment_error.data),
        )
        self.assertNotIn("DoesNotExist", str(payment_error.data))

    def test_declining_balance_depreciation_uses_configured_annual_rate(self):
        payload = self._payload()
        payload.update({
            "depreciation_method": "declining_balance",
            "declining_rate_percentage": "25.0000",
        })
        created = self.client.post("/api/assets/fixed-assets/", payload, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        result = self.client.post(f"/api/assets/fixed-assets/{created.data['id']}/depreciate/", {
            "period_date": "2026-02-28", "fiscal_year": self.year.pk,
        }, format="json")
        self.assertEqual(result.status_code, 201, result.data)
        self.assertEqual(result.data["depreciation_amount"], Decimal("4109.59"))

    def test_units_of_production_depreciation_requires_and_records_actual_units(self):
        payload = self._payload()
        payload.update({
            "depreciation_method": "units_of_production",
            "production_units_total": 36000,
        })
        created = self.client.post("/api/assets/fixed-assets/", payload, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        asset_id = created.data["id"]
        missing_units = self.client.post(f"/api/assets/fixed-assets/{asset_id}/depreciate/", {
            "period_date": "2026-02-28", "fiscal_year": self.year.pk,
        }, format="json")
        self.assertEqual(missing_units.status_code, 400, missing_units.data)
        result = self.client.post(f"/api/assets/fixed-assets/{asset_id}/depreciate/", {
            "period_date": "2026-02-28", "fiscal_year": self.year.pk, "units_produced": 9000,
        }, format="json")
        self.assertEqual(result.status_code, 201, result.data)
        self.assertEqual(result.data["depreciation_amount"], Decimal("27000.0000"))
        depreciation = asset_id and FixedAsset.objects.get(pk=asset_id).depreciations.get()
        self.assertEqual(depreciation.units_produced, 9000)

    def test_component_insurance_and_physical_inventory_control_records_are_available(self):
        created = self.client.post("/api/assets/fixed-assets/", self._payload(), format="json")
        self.assertEqual(created.status_code, 201, created.data)
        asset_id = created.data["id"]
        component = self.client.post("/api/assets/asset-components/", {
            "asset": asset_id, "code": "ENGINE", "name_ar": "المحرك",
            "original_cost": "30000.0000", "salvage_value": "0.0000",
            "useful_life_months": 60, "depreciation_method": "straight_line",
            "asset_account": self.asset_gl.pk,
            "accumulated_depreciation_account": self.accum_gl.pk,
            "depreciation_expense_account": self.expense_gl.pk,
        }, format="json")
        self.assertEqual(component.status_code, 201, component.data)
        policy = self.client.post("/api/assets/asset-insurance/", {
            "asset": asset_id, "policy_number": "POL-FA-01", "insurer_name": "شركة التأمين",
            "coverage_description": "تغطية شاملة", "start_date": "2026-01-01",
            "end_date": "2026-12-31", "insured_value": "120000.0000",
            "premium_amount": "2500.0000", "status": "active",
        }, format="json")
        self.assertEqual(policy.status_code, 201, policy.data)
        count = self.client.post("/api/assets/asset-physical-inventory/", {
            "asset": asset_id, "count_date": "2026-06-30",
            "expected_location": "المقر الرئيسي", "counted_location": "المقر الرئيسي",
            "expected_custodian": "مدير الحركة", "counted_custodian": "مدير الحركة",
            "result": "matched", "condition_notes": "سليم",
        }, format="json")
        self.assertEqual(count.status_code, 201, count.data)
        self.assertEqual(count.data["counted_by"], self.user.pk)

    def test_component_depreciation_posts_component_rates_and_reconciles_parent_ledger(self):
        from assets.models import AssetComponent, AssetComponentDepreciation, AssetDepreciation

        created = self.client.post("/api/assets/fixed-assets/", self._payload(), format="json")
        self.assertEqual(created.status_code, 201, created.data)
        asset_id = created.data["id"]
        shared_accounts = {
            "asset_account": self.asset_gl.pk,
            "accumulated_depreciation_account": self.accum_gl.pk,
            "depreciation_expense_account": self.expense_gl.pk,
        }
        for payload in (
            {
                "asset": asset_id, "code": "BODY", "name_ar": "هيكل السيارة",
                "original_cost": "60000.0000", "salvage_value": "6000.0000",
                "useful_life_months": 12, "depreciation_method": "straight_line",
                **shared_accounts,
            },
            {
                "asset": asset_id, "code": "ENGINE", "name_ar": "محرك السيارة",
                "original_cost": "60000.0000", "salvage_value": "6000.0000",
                "useful_life_months": 36, "depreciation_method": "declining_balance",
                "declining_rate_percentage": "25.0000",
                **shared_accounts,
            },
        ):
            response = self.client.post("/api/assets/asset-components/", payload, format="json")
            self.assertEqual(response.status_code, 201, response.data)
        depreciation = self.client.post(f"/api/assets/fixed-assets/{asset_id}/depreciate/", {
            "period_date": "2026-02-28", "fiscal_year": self.year.pk,
        }, format="json")
        self.assertEqual(depreciation.status_code, 201, depreciation.data)
        self.assertEqual(depreciation.data["depreciation_amount"], Decimal("9554.79"))
        self.assertEqual(AssetComponentDepreciation.objects.count(), 2)
        self.assertEqual(AssetDepreciation.objects.filter(asset_id=asset_id).count(), 1)
        self.assertEqual(sum((row.amount for row in AssetComponentDepreciation.objects.all()), Decimal("0")), Decimal("9554.7900"))
        self.assertEqual(AssetComponent.objects.filter(asset_id=asset_id).count(), 2)
        reconciliation = self.client.get(
            f"/api/assets/fixed-assets/account-reconciliation/?company={self.company.pk}"
            "&as_of=2026-02-28&date_from=2026-01-01&date_to=2026-02-28"
        )
        self.assertEqual(reconciliation.status_code, 200, reconciliation.data)
        self.assertTrue(reconciliation.data["is_reconciled"], reconciliation.data)
        self.assertEqual(reconciliation.data["difference_count"], 0)

