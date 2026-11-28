from datetime import date
from io import BytesIO

from openpyxl import Workbook
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APITestCase

from audit.models import AuditEvent
from django.utils import timezone

from accounting.models import AccountRole, ChartOfAccount, JournalEntry, JournalLine
from accounting.services import post_journal
from approvals.models import DiscountApprovalPolicy, FinancialApprovalRequest
from companies.models import Branch, Company, CompanyMembership, Currency, ExchangeRate, FiscalYear
from treasury.models import TreasuryAccount, TreasuryTransaction, TreasuryCheque, TreasuryFxPosition, BankReconciliation, BankStatementLine, BankStatementMatchAllocation, PettyCashFund, PettyCashExpense, PettyCashSettlement, PettyCashCount, PettyCashCustodianHandover, PettyCashTransfer


class TreasuryWorkflowTests(APITestCase):
    def setUp(self):
        currency = Currency.objects.create(code="EGP", name_ar="جنيه مصري")
        self.company = Company.objects.create(code="TRS", name_ar="شركة الخزينة", country="EG", activity="commercial", base_currency=currency)
        self.branch = Branch.objects.create(company=self.company, code="MAIN", name_ar="الرئيسي", is_head_office=True)
        self.year = FiscalYear.objects.create(company=self.company, name="2026", start_date=date(2026, 1, 1), end_date=date(2026, 12, 31))
        self.user = get_user_model().objects.create_user(username="treasury-user", password="test-password")
        CompanyMembership.objects.create(user=self.user, company=self.company, role="accountant", all_branches=True)
        self.client.force_authenticate(self.user)
        self.cash_gl = ChartOfAccount.objects.create(company=self.company, code="1100", name_ar="الصندوق", nature=ChartOfAccount.Nature.ASSET)
        self.bank_gl = ChartOfAccount.objects.create(company=self.company, code="1110", name_ar="البنك", nature=ChartOfAccount.Nature.ASSET)
        self.revenue_gl = ChartOfAccount.objects.create(company=self.company, code="4100", name_ar="إيرادات أخرى", nature=ChartOfAccount.Nature.REVENUE)
        self.cash = TreasuryAccount.objects.create(company=self.company, branch=self.branch, account=self.cash_gl, kind="cash", code="CASH-1", name_ar="الخزينة الرئيسية")
        self.bank = TreasuryAccount.objects.create(company=self.company, branch=self.branch, account=self.bank_gl, kind="bank", code="BANK-1", name_ar="البنك الرئيسي", bank_name="بنك تجريبي")

    def _branch_limited_treasury_user(self):
        other_branch = Branch.objects.create(
            company=self.company, code="BR2", name_ar="فرع ثانٍ",
        )
        limited = get_user_model().objects.create_user(
            username="treasury-branch-limited", password="test-password",
        )
        membership = CompanyMembership.objects.create(
            user=limited, company=self.company, role="accountant", all_branches=False,
        )
        membership.branches.add(self.branch)
        self.assertNotIn(other_branch.pk, membership.branches.values_list("id", flat=True))
        self.client.force_authenticate(limited)
        return limited

    def test_branch_limited_user_cannot_create_company_wide_treasury_account(self):
        self._branch_limited_treasury_user()
        global_gl = ChartOfAccount.objects.create(
            company=self.company, code="1199", name_ar="خزينة عامة",
            nature=ChartOfAccount.Nature.ASSET,
        )
        response = self.client.post("/api/treasury/accounts/", {
            "company": self.company.pk, "branch": None, "account": global_gl.pk,
            "kind": "cash", "code": "CASH-GLOBAL", "name_ar": "خزينة عامة",
        }, format="json")
        self.assertEqual(response.status_code, 403, response.data)
        self.assertFalse(TreasuryAccount.objects.filter(code="CASH-GLOBAL").exists())

    def test_branch_limited_user_cannot_move_branch_treasury_account_to_company_wide_scope(self):
        self._branch_limited_treasury_user()
        response = self.client.patch(
            f"/api/treasury/accounts/{self.cash.pk}/",
            {"branch": None},
            format="json",
        )
        self.assertEqual(response.status_code, 403, response.data)
        self.cash.refresh_from_db()
        self.assertEqual(self.cash.branch_id, self.branch.pk)

    def test_branch_limited_user_cannot_use_another_branches_treasury_account(self):
        other_branch = Branch.objects.create(
            company=self.company, code="BR2", name_ar="فرع آخر",
        )
        other_gl = ChartOfAccount.objects.create(
            company=self.company, code="1112", name_ar="بنك الفرع الآخر",
            nature=ChartOfAccount.Nature.ASSET,
        )
        other_bank = TreasuryAccount.objects.create(
            company=self.company, branch=other_branch, account=other_gl,
            kind="bank", code="BANK-OTHER-BRANCH", name_ar="بنك فرع آخر",
        )
        limited = get_user_model().objects.create_user(
            username="treasury-source-branch-limited", password="test-password",
        )
        membership = CompanyMembership.objects.create(
            user=limited, company=self.company, role="accountant", all_branches=False,
        )
        membership.branches.add(self.branch)
        self.client.force_authenticate(limited)
        response = self.client.post("/api/treasury/transactions/", {
            "company": self.company.pk, "branch": self.branch.pk, "fiscal_year": self.year.pk,
            "source_account": other_bank.pk, "counter_account": self.revenue_gl.pk,
            "kind": "receipt", "document_number": "TR-CROSS-BRANCH",
            "transaction_date": "2026-03-15", "amount": "25.0000",
            "description": "اختبار نطاق الفرع",
        }, format="json")
        self.assertEqual(response.status_code, 403, response.data)
        self.assertFalse(TreasuryTransaction.objects.filter(document_number="TR-CROSS-BRANCH").exists())

    def _create(self, kind="receipt", **extra):
        payload = {
            "company": self.company.pk, "branch": self.branch.pk, "fiscal_year": self.year.pk,
            "source_account": self.cash.pk, "counter_account": self.revenue_gl.pk,
            "kind": kind, "document_number": "TR-DOC-001", "transaction_date": "2026-03-15",
            "amount": "250.0000", "description": "اختبار حركة خزينة",
        }
        payload.update(extra)
        return self.client.post("/api/treasury/transactions/", payload, format="json")

    def test_treasury_account_rejects_currency_missing_from_reference_data(self):
        foreign_gl = ChartOfAccount.objects.create(
            company=self.company, code="1190", name_ar="حساب بعملة غير معرفة",
            nature=ChartOfAccount.Nature.ASSET,
        )
        response = self.client.post("/api/treasury/accounts/", {
            "company": self.company.pk, "branch": self.branch.pk, "account": foreign_gl.pk,
            "kind": "bank", "code": "BANK-UNKNOWN", "name_ar": "بنك بعملة غير معرفة",
            "currency_code": "XYZ",
        }, format="json")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("currency_code", response.data)
        self.assertFalse(TreasuryAccount.objects.filter(code="BANK-UNKNOWN").exists())

    def test_treasury_account_accepts_only_active_foreign_currency_reference(self):
        usd = Currency.objects.create(code="USD", name_ar="دولار أمريكي", is_active=True)
        foreign_gl = ChartOfAccount.objects.create(
            company=self.company, code="1191", name_ar="حساب دولار",
            nature=ChartOfAccount.Nature.ASSET,
        )
        response = self.client.post("/api/treasury/accounts/", {
            "company": self.company.pk, "branch": self.branch.pk, "account": foreign_gl.pk,
            "kind": "bank", "code": "BANK-USD-REF", "name_ar": "بنك دولار",
            "currency_code": usd.code,
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["currency_code"], "USD")

        eur = Currency.objects.create(code="EUR", name_ar="يورو", is_active=False)
        inactive_gl = ChartOfAccount.objects.create(
            company=self.company, code="1192", name_ar="حساب يورو",
            nature=ChartOfAccount.Nature.ASSET,
        )
        inactive_response = self.client.post("/api/treasury/accounts/", {
            "company": self.company.pk, "branch": self.branch.pk, "account": inactive_gl.pk,
            "kind": "bank", "code": "BANK-EUR-INACTIVE", "name_ar": "بنك يورو موقوف",
            "currency_code": eur.code,
        }, format="json")
        self.assertEqual(inactive_response.status_code, 400, inactive_response.data)
        self.assertIn("currency_code", inactive_response.data)
        self.assertFalse(TreasuryAccount.objects.filter(code="BANK-EUR-INACTIVE").exists())

    def test_opening_balance_creates_balanced_opening_journal_and_is_counted_once(self):
        retained = ChartOfAccount.objects.create(
            company=self.company, code="3000", name_ar="الأرباح المحتجزة",
            nature=ChartOfAccount.Nature.EQUITY,
        )
        AccountRole.objects.create(
            company=self.company, role=AccountRole.Role.RETAINED_EARNINGS, account=retained,
        )
        opening_bank_gl = ChartOfAccount.objects.create(
            company=self.company, code="1120", name_ar="بنك افتتاحي", nature=ChartOfAccount.Nature.ASSET,
        )
        created = self.client.post("/api/treasury/accounts/", {
            "company": self.company.pk, "branch": self.branch.pk, "account": opening_bank_gl.pk,
            "kind": "bank", "code": "BANK-OPEN", "name_ar": "بنك برصيد افتتاحي",
            "currency_code": "EGP", "opening_balance": "50000",
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        self.assertEqual(created.data["opening_balance"], "50000.0000")
        self.assertIsNotNone(created.data["opening_journal_entry"])
        entry = JournalEntry.objects.get(pk=created.data["opening_journal_entry"])
        self.assertEqual(entry.source_type, JournalEntry.Source.OPENING)
        self.assertEqual(entry.lines.get(account=opening_bank_gl).debit, Decimal("50000.0000"))
        self.assertEqual(entry.lines.get(account=retained).credit, Decimal("50000.0000"))

        payment = self.client.post("/api/treasury/transactions/", {
            "company": self.company.pk, "branch": self.branch.pk, "fiscal_year": self.year.pk,
            "source_account": created.data["id"], "counter_account": self.revenue_gl.pk,
            "kind": "payment", "document_number": "TR-OPEN-SPEND", "transaction_date": "2026-03-15",
            "amount": "50000", "description": "اختبار استخدام الرصيد الافتتاحي",
        }, format="json")
        self.assertEqual(payment.status_code, 201, payment.data)
        posted = self.client.post(
            f"/api/treasury/transactions/{payment.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(posted.status_code, 200, posted.data)
        self.assertEqual(JournalEntry.objects.filter(pk=entry.pk).count(), 1)

    def test_foreign_treasury_opening_balance_posts_fx_snapshot_and_initializes_position(self):
        usd = Currency.objects.create(code="USD", name_ar="دولار أمريكي")
        ExchangeRate.objects.create(
            company=self.company, currency=usd,
            rate_date=date(2025, 12, 31), rate=Decimal("30"),
        )
        retained = ChartOfAccount.objects.create(
            company=self.company, code="3000", name_ar="الأرباح المحتجزة",
            nature=ChartOfAccount.Nature.EQUITY,
        )
        AccountRole.objects.create(
            company=self.company, role=AccountRole.Role.RETAINED_EARNINGS, account=retained,
        )
        opening_gl = ChartOfAccount.objects.create(
            company=self.company, code="1120", name_ar="بنك دولار افتتاحي",
            nature=ChartOfAccount.Nature.ASSET,
        )
        created = self.client.post("/api/treasury/accounts/", {
            "company": self.company.pk, "branch": self.branch.pk, "account": opening_gl.pk,
            "kind": "bank", "code": "BANK-USD-OPEN", "name_ar": "بنك دولار افتتاحي",
            "currency_code": "USD", "opening_balance": "150000",
            "opening_foreign_amount": "5000", "opening_exchange_rate": "30",
            "opening_exchange_rate_date": "2025-12-31",
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        self.assertEqual(created.data["opening_foreign_amount"], "5000.0000")
        self.assertEqual(created.data["opening_balance"], "150000.0000")
        entry = JournalEntry.objects.get(pk=created.data["opening_journal_entry"])
        bank_line = entry.lines.get(account=opening_gl)
        self.assertEqual(bank_line.debit, Decimal("150000.0000"))
        self.assertEqual(bank_line.transaction_amount, Decimal("5000.0000"))
        self.assertEqual(bank_line.transaction_currency, usd)
        self.assertEqual(bank_line.exchange_rate, Decimal("30"))
        self.assertEqual(entry.lines.get(account=retained).credit, Decimal("150000.0000"))
        position = TreasuryFxPosition.objects.get(account_id=created.data["id"])
        self.assertEqual(position.foreign_amount, Decimal("5000.0000"))
        self.assertEqual(position.carrying_base_amount, Decimal("150000.0000"))

        ExchangeRate.objects.create(
            company=self.company, currency=usd,
            rate_date=date(2026, 3, 15), rate=Decimal("30"),
        )
        payment = TreasuryTransaction.objects.create(
            company=self.company, branch=self.branch, fiscal_year=self.year,
            source_account_id=created.data["id"], counter_account=self.revenue_gl,
            kind="payment", document_number="TR-USD-OPEN-SPEND",
            transaction_date=date(2026, 3, 15), amount=Decimal("30000"),
            transaction_currency=usd, transaction_amount=Decimal("1000"),
            exchange_rate=Decimal("30"), exchange_rate_date=date(2026, 3, 15),
            description="استخدام جزء من الرصيد الافتتاحي الأجنبي",
        )
        from treasury.services import post_treasury_transaction
        post_treasury_transaction(payment.pk, user=self.user)
        position.refresh_from_db()
        self.assertEqual(position.foreign_amount, Decimal("4000.0000"))
        self.assertEqual(position.carrying_base_amount, Decimal("120000.0000"))

    def test_foreign_treasury_opening_balance_rejects_unapproved_rate(self):
        Currency.objects.create(code="USD", name_ar="دولار أمريكي")
        opening_gl = ChartOfAccount.objects.create(
            company=self.company, code="1121", name_ar="بنك دولار غير معتمد",
            nature=ChartOfAccount.Nature.ASSET,
        )
        response = self.client.post("/api/treasury/accounts/", {
            "company": self.company.pk, "branch": self.branch.pk, "account": opening_gl.pk,
            "kind": "bank", "code": "BANK-USD-BAD", "name_ar": "بنك دولار",
            "currency_code": "USD", "opening_balance": "150000",
            "opening_foreign_amount": "5000", "opening_exchange_rate": "30",
            "opening_exchange_rate_date": "2025-12-31",
        }, format="json")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(TreasuryAccount.objects.filter(code="BANK-USD-BAD").exists())

    def test_receipt_posts_balanced_journal_and_is_idempotent(self):
        response = self._create()
        self.assertEqual(response.status_code, 201, response.data)
        doc_id = response.data["id"]
        posted = self.client.post(f"/api/treasury/transactions/{doc_id}/post/", {}, format="json")
        self.assertEqual(posted.status_code, 200, posted.data)
        entry = JournalEntry.objects.get(pk=posted.data["journal_entry"])
        self.assertEqual(entry.status, JournalEntry.Status.POSTED)
        lines = list(entry.lines.order_by("debit"))
        self.assertEqual(sum((line.debit for line in lines), Decimal("0")), Decimal("250.0000"))
        self.assertEqual(sum((line.credit for line in lines), Decimal("0")), Decimal("250.0000"))
        again = self.client.post(f"/api/treasury/transactions/{doc_id}/post/", {}, format="json")
        self.assertEqual(again.status_code, 400)
        self.assertEqual(JournalEntry.objects.filter(pk=entry.pk).count(), 1)

    def test_standalone_base_currency_receipt_reversal_updates_document_and_journal(self):
        created = self._create(document_number="TR-REV-RECEIPT-001")
        self.assertEqual(created.status_code, 201, created.data)
        posted = self.client.post(f"/api/treasury/transactions/{created.data['id']}/post/", {}, format="json")
        self.assertEqual(posted.status_code, 200, posted.data)
        original = JournalEntry.objects.get(pk=posted.data["journal_entry"])
        reversed_response = self.client.post(
            f"/api/treasury/transactions/{created.data['id']}/reverse/",
            {"entry_date": "2026-03-16"}, format="json",
        )
        self.assertEqual(reversed_response.status_code, 200, reversed_response.data)
        self.assertEqual(reversed_response.data["status"], TreasuryTransaction.Status.REVERSED)
        self.assertTrue(reversed_response.data["reversal_journal_number"])
        original.refresh_from_db()
        self.assertEqual(original.status, JournalEntry.Status.REVERSED)
        reversal = JournalEntry.objects.get(pk=reversed_response.data["reversal_journal_entry"])
        self.assertEqual(reversal.reversal_of_id, original.pk)
        self.assertEqual(reversal.status, JournalEntry.Status.POSTED)
        duplicate = self.client.post(
            f"/api/treasury/transactions/{created.data['id']}/reverse/",
            {"entry_date": "2026-03-17"}, format="json",
        )
        self.assertEqual(duplicate.status_code, 400, duplicate.data)
        cash_flow = self.client.get(f"/api/treasury/transactions/cash-flow/?company={self.company.pk}")
        self.assertEqual(cash_flow.status_code, 200, cash_flow.data)
        self.assertEqual(cash_flow.data["rows"], [])

    def test_payment_and_transfer_reversal_restore_both_treasury_balances(self):
        seed = self._create(document_number="TR-REV-SEED-001")
        self.assertEqual(seed.status_code, 201, seed.data)
        self.assertEqual(self.client.post(
            f"/api/treasury/transactions/{seed.data['id']}/post/", {}, format="json"
        ).status_code, 200)

        payment = self._create(
            kind="payment", document_number="TR-REV-PAY-001", amount="100.0000",
        )
        self.assertEqual(payment.status_code, 201, payment.data)
        payment_posted = self.client.post(
            f"/api/treasury/transactions/{payment.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(payment_posted.status_code, 200, payment_posted.data)
        payment_reversed = self.client.post(
            f"/api/treasury/transactions/{payment.data['id']}/reverse/",
            {"entry_date": "2026-03-16"}, format="json",
        )
        self.assertEqual(payment_reversed.status_code, 200, payment_reversed.data)
        self.assertEqual(payment_reversed.data["status"], TreasuryTransaction.Status.REVERSED)

        transfer = self._create(
            kind="transfer", counter_account=None, destination_account=self.bank.pk,
            document_number="TR-REV-TRANSFER-001", amount="50.0000",
        )
        self.assertEqual(transfer.status_code, 201, transfer.data)
        transfer_posted = self.client.post(
            f"/api/treasury/transactions/{transfer.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(transfer_posted.status_code, 200, transfer_posted.data)
        transfer_reversed = self.client.post(
            f"/api/treasury/transactions/{transfer.data['id']}/reverse/",
            {"entry_date": "2026-03-16"}, format="json",
        )
        self.assertEqual(transfer_reversed.status_code, 200, transfer_reversed.data)
        self.assertEqual(transfer_reversed.data["status"], TreasuryTransaction.Status.REVERSED)
        self.assertEqual(
            JournalEntry.objects.get(pk=transfer_reversed.data["reversal_journal_entry"]).lines.count(),
            2,
        )

    def test_treasury_reversal_rejects_foreign_currency_and_linked_operational_documents(self):
        usd = Currency.objects.create(code="USD", name_ar="دولار أمريكي")
        foreign_gl = ChartOfAccount.objects.create(company=self.company, code="1198", name_ar="بنك دولار", nature=ChartOfAccount.Nature.ASSET)
        foreign = TreasuryAccount.objects.create(company=self.company, branch=self.branch, account=foreign_gl, kind="bank", code="BANK-REV-USD", name_ar="بنك دولار", currency_code="USD")
        fx_rate = ExchangeRate.objects.create(company=self.company, currency=usd, rate_date=date(2026, 3, 15), rate=Decimal("50"))
        foreign_entry = JournalEntry.objects.create(company=self.company, branch=self.branch, fiscal_year=self.year, entry_number="TR-REV-USD-JE", entry_date=date(2026, 3, 15), description="قيد حركة أجنبية", source_type=JournalEntry.Source.MANUAL)
        JournalLine.objects.bulk_create([
            JournalLine(entry=foreign_entry, account=foreign_gl, debit=Decimal("500"), credit=0),
            JournalLine(entry=foreign_entry, account=self.revenue_gl, debit=0, credit=Decimal("500")),
        ])
        post_journal(foreign_entry.pk)
        foreign_doc = TreasuryTransaction.objects.create(
            company=self.company, branch=self.branch, fiscal_year=self.year, source_account=foreign,
            counter_account=self.revenue_gl, kind=TreasuryTransaction.Kind.RECEIPT,
            status=TreasuryTransaction.Status.POSTED, document_number="TR-REV-USD",
            transaction_date=date(2026, 3, 15), amount=Decimal("500"), transaction_currency=usd,
            transaction_amount=Decimal("10"), exchange_rate=fx_rate.rate, exchange_rate_date=fx_rate.rate_date,
            description="حركة أجنبية", journal_entry=foreign_entry,
        )
        rejected_fx = self.client.post(f"/api/treasury/transactions/{foreign_doc.pk}/reverse/", {"entry_date": "2026-03-16"}, format="json")
        self.assertEqual(rejected_fx.status_code, 400, rejected_fx.data)
        operational = JournalEntry.objects.create(
            company=self.company, branch=self.branch, fiscal_year=self.year, entry_number="TR-REV-LINKED-001",
            entry_date=date(2026, 3, 15), description="حركة بنك مرتبطة بمبيعات", source_type=JournalEntry.Source.SALES,
        )
        JournalLine.objects.bulk_create([
            JournalLine(entry=operational, account=self.cash_gl, debit=Decimal("75"), credit=0),
            JournalLine(entry=operational, account=self.revenue_gl, debit=0, credit=Decimal("75")),
        ])
        post_journal(operational.pk)
        linked = TreasuryTransaction.objects.create(
            company=self.company, branch=self.branch, fiscal_year=self.year, source_account=self.cash,
            counter_account=self.revenue_gl, kind=TreasuryTransaction.Kind.RECEIPT,
            status=TreasuryTransaction.Status.POSTED, document_number="TR-REV-LINKED",
            transaction_date=date(2026, 3, 15), amount=Decimal("75"), description="مرآة حركة مبيعات",
            journal_entry=operational,
        )
        rejected_linked = self.client.post(f"/api/treasury/transactions/{linked.pk}/reverse/", {"entry_date": "2026-03-16"}, format="json")
        self.assertEqual(rejected_linked.status_code, 400, rejected_linked.data)
        linked.refresh_from_db()
        self.assertEqual(linked.status, TreasuryTransaction.Status.POSTED)

    def test_foreign_currency_treasury_draft_requires_exchange_rate_snapshot(self):
        foreign_gl = ChartOfAccount.objects.create(
            company=self.company, code="1120", name_ar="بنك بالدولار",
            nature=ChartOfAccount.Nature.ASSET,
        )
        foreign_bank = TreasuryAccount.objects.create(
            company=self.company, branch=self.branch, account=foreign_gl,
            kind="bank", code="BANK-USD", name_ar="حساب دولار",
            currency_code="USD",
        )
        response = self._create(
            source_account=foreign_bank.pk,
            document_number="TR-USD-001",
        )

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("سعر الصرف", str(response.data))
        self.assertFalse(TreasuryTransaction.objects.filter(document_number="TR-USD-001").exists())
        self.assertEqual(JournalEntry.objects.count(), 0)
        foreign_bank.refresh_from_db()
        self.assertTrue(foreign_bank.is_active)

    def test_treasury_account_currency_and_branch_are_immutable_after_posted_activity(self):
        created = self._create(document_number="TR-HISTORY-001")
        self.assertEqual(created.status_code, 201, created.data)
        posted = self.client.post(
            f"/api/treasury/transactions/{created.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(posted.status_code, 200, posted.data)

        currency_change = self.client.patch(
            f"/api/treasury/accounts/{self.cash.pk}/",
            {"currency_code": "USD"}, format="json",
        )
        self.assertEqual(currency_change.status_code, 400, currency_change.data)
        branch = Branch.objects.create(company=self.company, code="HIST-BR", name_ar="فرع تاريخي")
        branch_change = self.client.patch(
            f"/api/treasury/accounts/{self.cash.pk}/",
            {"branch": branch.pk}, format="json",
        )
        self.assertEqual(branch_change.status_code, 400, branch_change.data)

        self.cash.refresh_from_db()
        self.assertEqual(self.cash.currency_code, "EGP")
        self.assertEqual(self.cash.branch_id, self.branch.pk)

    def test_transfer_posts_between_separate_cash_and_bank_ledgers(self):
        receipt = self._create(document_number="TR-SEED-001")
        self.assertEqual(receipt.status_code, 201, receipt.data)
        seeded = self.client.post(f"/api/treasury/transactions/{receipt.data['id']}/post/", {}, format="json")
        self.assertEqual(seeded.status_code, 200, seeded.data)
        response = self._create(
            kind="transfer", counter_account=None, destination_account=self.bank.pk,
            document_number="TR-DOC-002",
        )
        self.assertEqual(response.status_code, 201, response.data)
        posted = self.client.post(f"/api/treasury/transactions/{response.data['id']}/post/", {}, format="json")
        self.assertEqual(posted.status_code, 200, posted.data)
        entry = JournalEntry.objects.get(pk=posted.data["journal_entry"])
        self.assertEqual(set(entry.lines.values_list("account_id", flat=True)), {self.cash_gl.pk, self.bank_gl.pk})


    def test_limited_branch_user_cannot_view_company_wide_bank_statement_lines(self):
        shared_gl = ChartOfAccount.objects.create(
            company=self.company, code="1126", name_ar="بنك عام للكشف",
            nature=ChartOfAccount.Nature.ASSET,
        )
        shared_bank = TreasuryAccount.objects.create(
            company=self.company, branch=None, account=shared_gl, kind="bank",
            code="BANK-STATEMENT-SHARED", name_ar="بنك كشف عام",
        )
        Branch.objects.create(company=self.company, code="OTHER-STATEMENT", name_ar="فرع آخر")
        reconciliation = BankReconciliation.objects.create(
            company=self.company, bank_account=shared_bank,
            statement_number="SHARED-ST-001", statement_date=date(2026, 3, 31),
            opening_balance=Decimal("0"), closing_balance=Decimal("125"),
            created_by=self.user,
        )
        line = BankStatementLine.objects.create(
            reconciliation=reconciliation, line_number=1,
            transaction_date=date(2026, 3, 15), description="حركة كشف خاصة",
            reference="PRIVATE-REF", amount=Decimal("125"),
        )

        limited = get_user_model().objects.create_user(
            username="treasury-statement-limited", password="test-password"
        )
        membership = CompanyMembership.objects.create(
            user=limited, company=self.company, role="accountant", all_branches=False,
        )
        membership.branches.add(self.branch)
        self.client.force_authenticate(limited)

        reconciliations = self.client.get("/api/treasury/reconciliations/")
        self.assertEqual(reconciliations.status_code, 200, reconciliations.data)
        reconciliation_rows = reconciliations.data["results"] if isinstance(reconciliations.data, dict) else reconciliations.data
        self.assertNotIn(reconciliation.pk, [row["id"] for row in reconciliation_rows])
        self.assertEqual(
            self.client.get(f"/api/treasury/reconciliations/{reconciliation.pk}/").status_code,
            403,
        )
        self.assertEqual(
            self.client.get(f"/api/treasury/reconciliations/{reconciliation.pk}/summary/").status_code,
            403,
        )

        statement_lines = self.client.get("/api/treasury/statement-lines/")
        self.assertEqual(statement_lines.status_code, 200, statement_lines.data)
        line_rows = statement_lines.data["results"] if isinstance(statement_lines.data, dict) else statement_lines.data
        self.assertNotIn(line.pk, [row["id"] for row in line_rows])
        self.assertEqual(self.client.get(f"/api/treasury/statement-lines/{line.pk}/").status_code, 404)

    def test_limited_branch_user_cannot_view_company_wide_treasury_account(self):
        shared_gl = ChartOfAccount.objects.create(
            company=self.company, code="1125", name_ar="بنك عام", nature=ChartOfAccount.Nature.ASSET,
        )
        shared = TreasuryAccount.objects.create(
            company=self.company, branch=None, account=shared_gl, kind="bank",
            code="BANK-COMPANY-WIDE", name_ar="بنك عام",
        )
        # A second active branch makes this membership genuinely branch-limited.
        Branch.objects.create(company=self.company, code="OTHER-SHARED", name_ar="فرع آخر")
        limited = get_user_model().objects.create_user(
            username="treasury-shared-bank-limited", password="test-password"
        )
        membership = CompanyMembership.objects.create(
            user=limited, company=self.company, role="accountant", all_branches=False,
        )
        membership.branches.add(self.branch)
        self.client.force_authenticate(limited)
        response = self.client.get(f"/api/treasury/accounts/{shared.pk}/")
        self.assertEqual(response.status_code, 404, response.data)

    def test_limited_branch_user_cannot_reassign_treasury_account_to_another_branch(self):
        other_branch = Branch.objects.create(company=self.company, code="OTHER", name_ar="فرع آخر")
        limited = get_user_model().objects.create_user(username="treasury-branch-limited", password="test-password")
        membership = CompanyMembership.objects.create(
            user=limited, company=self.company, role="accountant", all_branches=False
        )
        membership.branches.add(self.branch)
        self.client.force_authenticate(limited)
        response = self.client.patch(
            f"/api/treasury/accounts/{self.bank.pk}/",
            {"branch": other_branch.pk},
            format="json",
        )
        self.assertEqual(response.status_code, 403, response.data)
        self.bank.refresh_from_db()
        self.assertEqual(self.bank.branch_id, self.branch.pk)

    def test_limited_branch_user_cannot_reassign_draft_transaction_to_another_branch(self):
        created = self._create(document_number="TR-BRANCH-LOCK-001")
        self.assertEqual(created.status_code, 201, created.data)
        other_branch = Branch.objects.create(company=self.company, code="OTHER2", name_ar="فرع آخر")
        limited = get_user_model().objects.create_user(username="treasury-branch-limited-2", password="test-password")
        membership = CompanyMembership.objects.create(
            user=limited, company=self.company, role="accountant", all_branches=False
        )
        membership.branches.add(self.branch)
        self.client.force_authenticate(limited)
        response = self.client.patch(
            f"/api/treasury/transactions/{created.data['id']}/",
            {"branch": other_branch.pk},
            format="json",
        )
        self.assertEqual(response.status_code, 403, response.data)
        doc = TreasuryTransaction.objects.get(pk=created.data["id"])
        self.assertEqual(doc.branch_id, self.branch.pk)

    def test_posted_treasury_transaction_cannot_be_deleted(self):
        created = self._create(document_number="TR-POSTED-DELETE-001")
        self.assertEqual(created.status_code, 201, created.data)
        posted = self.client.post(
            f"/api/treasury/transactions/{created.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(posted.status_code, 200, posted.data)
        deleted = self.client.delete(f"/api/treasury/transactions/{created.data['id']}/")
        self.assertEqual(deleted.status_code, 400, deleted.data)
        self.assertTrue(TreasuryTransaction.objects.filter(pk=created.data["id"]).exists())

    def _post_cash_ledger_movement(self, number, movement_date, debit, credit):
        debit = Decimal(debit)
        credit = Decimal(credit)
        offset = ChartOfAccount.objects.create(
            company=self.company, code=f"TEST-{number}",
            name_ar="مقابل حركة خزينة اختبار",
            nature=(
                ChartOfAccount.Nature.EXPENSE
                if credit else ChartOfAccount.Nature.REVENUE
            ),
        )
        entry = JournalEntry.objects.create(
            company=self.company, branch=self.branch, fiscal_year=self.year,
            entry_number=number, entry_date=movement_date,
            description="حركة اختبار مؤرخة على حساب الخزينة",
        )
        JournalLine.objects.create(
            entry=entry, account=self.cash_gl, debit=debit, credit=credit,
        )
        JournalLine.objects.create(
            entry=entry, account=offset, debit=credit, credit=debit,
        )
        return post_journal(entry.pk, actor=self.user)

    def test_backdated_treasury_payment_cannot_use_a_future_deposit(self):
        self._post_cash_ledger_movement(
            "FUTURE-DEPOSIT", date(2026, 3, 20), "500", "0",
        )
        payment = self._create(
            kind="payment", document_number="BACKDATED-NO-FUTURE-FUND",
            transaction_date="2026-03-15", amount="100",
        )
        self.assertEqual(payment.status_code, 201, payment.data)
        journal_count = JournalEntry.objects.count()

        rejected = self.client.post(
            f"/api/treasury/transactions/{payment.data['id']}/post/", {}, format="json",
        )

        self.assertEqual(rejected.status_code, 400, rejected.data)
        self.assertIn("تاريخ الحركة", str(rejected.data))
        self.assertEqual(JournalEntry.objects.count(), journal_count)

    def test_backdated_treasury_payment_rejects_intermediate_future_overdraft(self):
        self.cash.opening_balance = Decimal("200")
        self.cash.save(update_fields=("opening_balance",))
        self._post_cash_ledger_movement(
            "FUTURE-WITHDRAWAL", date(2026, 3, 20), "0", "180",
        )
        self._post_cash_ledger_movement(
            "FUTURE-DEPOSIT-AFTER-OUT", date(2026, 3, 25), "100", "0",
        )
        payment = self._create(
            kind="payment", document_number="BACKDATED-FUTURE-OVERDRAFT",
            transaction_date="2026-03-15", amount="100",
        )
        self.assertEqual(payment.status_code, 201, payment.data)
        journal_count = JournalEntry.objects.count()

        rejected = self.client.post(
            f"/api/treasury/transactions/{payment.data['id']}/post/", {}, format="json",
        )

        self.assertEqual(rejected.status_code, 400, rejected.data)
        self.assertIn("حركة لاحقة", str(rejected.data))
        self.assertEqual(JournalEntry.objects.count(), journal_count)

    def test_payment_requires_counter_account_and_positive_amount(self):
        invalid = self._create(kind="payment", amount="0")
        self.assertEqual(invalid.status_code, 400)

    def test_payment_posting_blocks_negative_treasury_balance(self):
        response = self._create(kind="payment", document_number="TR-PAY-001")
        self.assertEqual(response.status_code, 201, response.data)
        posted = self.client.post(f"/api/treasury/transactions/{response.data['id']}/post/", {}, format="json")
        self.assertEqual(posted.status_code, 400)
        self.assertIn("الرصيد المتاح غير كافٍ", str(posted.data))
        self.assertEqual(JournalEntry.objects.count(), 0)

    def test_available_balance_counts_original_and_reversal_journal_together(self):
        self.cash.opening_balance = Decimal("1000.0000")
        self.cash.save(update_fields=("opening_balance",))
        original = JournalEntry.objects.create(
            company=self.company, branch=self.branch, fiscal_year=self.year,
            entry_number="CASH-MANUAL-REV-001", entry_date=date(2026, 3, 15),
            description="مصروف نقدي يدوي سيتم عكسه",
        )
        JournalLine.objects.bulk_create([
            JournalLine(entry=original, account=self.cash_gl, debit=Decimal("500.0000"), credit=0),
            JournalLine(entry=original, account=self.revenue_gl, debit=0, credit=Decimal("500.0000")),
        ])
        post_journal(original.pk)
        reversed_response = self.client.post(
            f"/api/accounting/journal-entries/{original.pk}/reverse/",
            {"entry_date": "2026-03-16"}, format="json",
        )
        self.assertEqual(reversed_response.status_code, 201, reversed_response.data)

        payment = self._create(
            kind="payment", document_number="TR-PAY-AFTER-REVERSAL",
            amount="800.0000",
        )
        self.assertEqual(payment.status_code, 201, payment.data)
        posted = self.client.post(
            f"/api/treasury/transactions/{payment.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(posted.status_code, 200, posted.data)



    def test_foreign_bank_reconciliation_matches_statement_in_foreign_currency(self):
        usd = Currency.objects.create(code="USD", name_ar="دولار أمريكي")
        ExchangeRate.objects.create(
            company=self.company, currency=usd,
            rate_date=date(2026, 3, 15), rate=Decimal("30"),
        )
        foreign_gl = ChartOfAccount.objects.create(
            company=self.company, code="1120", name_ar="بنك دولار",
            nature=ChartOfAccount.Nature.ASSET,
        )
        foreign_bank = TreasuryAccount.objects.create(
            company=self.company, branch=self.branch, account=foreign_gl,
            kind="bank", code="BANK-USD-RECON", name_ar="بنك دولار",
            currency_code="USD",
        )
        receipt = self._create(
            source_account=foreign_bank.pk, document_number="TR-USD-RECON",
            amount="3000.0000", transaction_currency=usd.pk,
            transaction_amount="100.0000", exchange_rate="30.0000000000",
            exchange_rate_date="2026-03-15",
        )
        self.assertEqual(receipt.status_code, 201, receipt.data)
        posted = self.client.post(
            f"/api/treasury/transactions/{receipt.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(posted.status_code, 200, posted.data)

        gain = ChartOfAccount.objects.create(
            company=self.company, code="4902", name_ar="أرباح فروق عملة غير محققة",
            nature=ChartOfAccount.Nature.REVENUE,
        )
        AccountRole.objects.create(
            company=self.company, role=AccountRole.Role.UNREALIZED_FX_GAIN, account=gain,
        )
        ExchangeRate.objects.create(
            company=self.company, currency=usd,
            rate_date=date(2026, 3, 31), rate=Decimal("31"),
        )
        revalued = self.client.post(
            f"/api/treasury/accounts/{foreign_bank.pk}/revalue/",
            {"fiscal_year": self.year.pk, "revaluation_date": "2026-03-31", "rate_date": "2026-03-31"},
            format="json",
        )
        self.assertEqual(revalued.status_code, 201, revalued.data)
        self.assertEqual(revalued.data["difference"], Decimal("100.0000"))
        account_detail = self.client.get(f"/api/treasury/accounts/{foreign_bank.pk}/")
        self.assertEqual(account_detail.status_code, 200, account_detail.data)
        self.assertEqual(account_detail.data["fx_position_foreign_amount"], Decimal("100.0000"))
        self.assertEqual(account_detail.data["fx_position_carrying_base_amount"], Decimal("3100.0000"))
        history = self.client.get(f"/api/treasury/accounts/{foreign_bank.pk}/revaluations/")
        self.assertEqual(history.status_code, 200, history.data)
        self.assertEqual(len(history.data), 1)
        self.assertEqual(history.data[0]["journal_number"], revalued.data["journal_number"])
        self.assertEqual(history.data[0]["status"], "posted")
        self.assertTrue(history.data[0]["can_reverse"])

        reversed_response = self.client.post(
            f"/api/treasury/accounts/{foreign_bank.pk}/reverse-revaluation/",
            {"revaluation_id": revalued.data["id"], "entry_date": "2026-04-01"},
            format="json",
        )
        self.assertEqual(reversed_response.status_code, 201, reversed_response.data)
        self.assertEqual(reversed_response.data["status"], "reversed")
        self.assertTrue(reversed_response.data["reversal_journal_number"])
        account_detail = self.client.get(f"/api/treasury/accounts/{foreign_bank.pk}/")
        self.assertEqual(account_detail.status_code, 200, account_detail.data)
        self.assertEqual(account_detail.data["fx_position_foreign_amount"], Decimal("100.0000"))
        self.assertEqual(account_detail.data["fx_position_carrying_base_amount"], Decimal("3000.0000"))
        history = self.client.get(f"/api/treasury/accounts/{foreign_bank.pk}/revaluations/")
        self.assertEqual(history.status_code, 200, history.data)
        self.assertEqual(history.data[0]["status"], "reversed")
        self.assertFalse(history.data[0]["can_reverse"])
        self.assertEqual(
            history.data[0]["reversal_journal_number"],
            reversed_response.data["reversal_journal_number"],
        )

        recon = self.client.post("/api/treasury/reconciliations/", {
            "company": self.company.pk, "bank_account": foreign_bank.pk,
            "statement_number": "USD-ST-001", "statement_date": "2026-03-31",
            "opening_balance": "0.0000", "closing_balance": "100.0000",
        }, format="json")
        self.assertEqual(recon.status_code, 201, recon.data)
        line = self.client.post(
            f"/api/treasury/reconciliations/{recon.data['id']}/add_line/",
            {"line_number": 1, "transaction_date": "2026-03-15",
             "description": "USD deposit", "reference": "USD-REF", "amount": "100.0000"},
            format="json",
        )
        self.assertEqual(line.status_code, 201, line.data)
        matched = self.client.post(
            f"/api/treasury/reconciliations/{recon.data['id']}/match/",
            {"line_id": line.data["id"], "transaction_id": posted.data["id"]},
            format="json",
        )
        self.assertEqual(matched.status_code, 200, matched.data)
        summary = self.client.get(f"/api/treasury/reconciliations/{recon.data['id']}/summary/")
        self.assertEqual(summary.status_code, 200, summary.data)
        self.assertEqual(summary.data["book_balance"], "100.0000")
        self.assertEqual(summary.data["unexplained_difference"], "0.0000")
        completed = self.client.post(
            f"/api/treasury/reconciliations/{recon.data['id']}/reconcile/", {}, format="json"
        )
        self.assertEqual(completed.status_code, 200, completed.data)

    def test_bank_reconciliation_matches_statement_and_posts_no_extra_journal(self):
        payload = {
            "company": self.company.pk, "branch": self.branch.pk, "fiscal_year": self.year.pk,
            "source_account": self.bank.pk, "counter_account": self.revenue_gl.pk,
            "kind": "receipt", "document_number": "BANK-REC-001", "transaction_date": "2026-03-10",
            "amount": "250.0000", "description": "تحصيل بنكي",
        }
        created = self.client.post("/api/treasury/transactions/", payload, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        posted = self.client.post(f"/api/treasury/transactions/{created.data['id']}/post/", {}, format="json")
        self.assertEqual(posted.status_code, 200, posted.data)
        recon = self.client.post("/api/treasury/reconciliations/", {
            "company": self.company.pk, "bank_account": self.bank.pk, "statement_number": "ST-001",
            "statement_date": "2026-03-31", "opening_balance": "0.0000", "closing_balance": "250.0000",
        }, format="json")
        self.assertEqual(recon.status_code, 201, recon.data)
        line = self.client.post(f"/api/treasury/reconciliations/{recon.data['id']}/add_line/", {
            "line_number": 1, "transaction_date": "2026-03-10", "description": "تحصيل",
            "reference": "REF-1", "amount": "250.0000",
        }, format="json")
        self.assertEqual(line.status_code, 201, line.data)
        matched = self.client.post(f"/api/treasury/reconciliations/{recon.data['id']}/match/", {
            "line_id": line.data["id"], "transaction_id": created.data["id"],
        }, format="json")
        self.assertEqual(matched.status_code, 200, matched.data)
        summary = self.client.get(f"/api/treasury/reconciliations/{recon.data['id']}/summary/")
        self.assertEqual(summary.status_code, 200, summary.data)
        self.assertEqual(summary.data["unexplained_difference"], "0.0000")
        completed = self.client.post(f"/api/treasury/reconciliations/{recon.data['id']}/reconcile/", {}, format="json")
        self.assertEqual(completed.status_code, 200, completed.data)
        self.assertEqual(completed.data["status"], "reconciled")
        self.assertEqual(JournalEntry.objects.count(), 1)

    def test_bank_statement_fee_creates_draft_payment_then_reconciles_after_posting(self):
        expense = ChartOfAccount.objects.create(
            company=self.company, code="6205", name_ar="عمولات بنكية",
            nature=ChartOfAccount.Nature.EXPENSE,
        )
        # Model the 250 EGP as a true opening balance before the statement
        # period, not as an unmatched transaction inside that period.
        retained = ChartOfAccount.objects.create(
            company=self.company, code="3000", name_ar="الأرباح المحتجزة",
            nature=ChartOfAccount.Nature.EQUITY,
        )
        AccountRole.objects.create(
            company=self.company, role=AccountRole.Role.RETAINED_EARNINGS, account=retained,
        )
        opening_bank_gl = ChartOfAccount.objects.create(
            company=self.company, code="1120", name_ar="بنك برصيد افتتاحي",
            nature=ChartOfAccount.Nature.ASSET,
        )
        opening_bank = self.client.post("/api/treasury/accounts/", {
            "company": self.company.pk, "branch": self.branch.pk, "account": opening_bank_gl.pk,
            "kind": "bank", "code": "BANK-FEE-OPEN", "name_ar": "بنك اختبار العمولة",
            "currency_code": "EGP", "opening_balance": "250.0000",
        }, format="json")
        self.assertEqual(opening_bank.status_code, 201, opening_bank.data)
        recon = self.client.post("/api/treasury/reconciliations/", {
            "company": self.company.pk, "bank_account": opening_bank.data["id"],
            "statement_number": "ST-FEE-001", "statement_date": "2026-03-31",
            "opening_balance": "250.0000", "closing_balance": "235.0000",
        }, format="json")
        self.assertEqual(recon.status_code, 201, recon.data)
        line = self.client.post(
            f"/api/treasury/reconciliations/{recon.data['id']}/add_line/",
            {"line_number": 1, "transaction_date": "2026-03-12",
             "description": "عمولة تحويل بنكي", "reference": "FEE-15", "amount": "-15.0000"},
            format="json",
        )
        self.assertEqual(line.status_code, 201, line.data)
        adjustment = self.client.post(
            f"/api/treasury/reconciliations/{recon.data['id']}/record-adjustment/",
            {"line_id": line.data["id"], "counter_account": expense.pk,
             "fiscal_year": self.year.pk},
            format="json",
        )
        self.assertEqual(adjustment.status_code, 201, adjustment.data)
        self.assertEqual(adjustment.data["transaction"]["kind"], "payment")
        self.assertEqual(adjustment.data["transaction"]["status"], "draft")
        self.assertEqual(adjustment.data["line"]["adjustment_document"], adjustment.data["transaction"]["document_number"])
        # Only the earlier funding receipt is posted; the adjustment remains a draft.
        self.assertEqual(JournalEntry.objects.count(), 1)

        posted = self.client.post(
            f"/api/treasury/transactions/{adjustment.data['transaction']['id']}/post/",
            {}, format="json",
        )
        self.assertEqual(posted.status_code, 200, posted.data)
        matched = self.client.post(
            f"/api/treasury/reconciliations/{recon.data['id']}/match/",
            {"line_id": line.data["id"], "transaction_id": posted.data["id"]},
            format="json",
        )
        self.assertEqual(matched.status_code, 200, matched.data)
        summary = self.client.get(
            f"/api/treasury/reconciliations/{recon.data['id']}/summary/"
        )
        self.assertEqual(summary.status_code, 200, summary.data)
        self.assertEqual(summary.data["unexplained_difference"], "0.0000")
        completed = self.client.post(
            f"/api/treasury/reconciliations/{recon.data['id']}/reconcile/",
            {}, format="json",
        )
        self.assertEqual(completed.status_code, 200, completed.data)
        self.assertEqual(completed.data["status"], "reconciled")

    def test_bank_statement_interest_creates_receipt_and_foreign_adjustments_are_rejected(self):
        recon = self.client.post("/api/treasury/reconciliations/", {
            "company": self.company.pk, "bank_account": self.bank.pk,
            "statement_number": "ST-INT-001", "statement_date": "2026-03-31",
            "opening_balance": "0.0000", "closing_balance": "8.0000",
        }, format="json")
        self.assertEqual(recon.status_code, 201, recon.data)
        line = self.client.post(
            f"/api/treasury/reconciliations/{recon.data['id']}/add_line/",
            {"line_number": 1, "transaction_date": "2026-03-20",
             "description": "فوائد دائنة", "reference": "INT-8", "amount": "8.0000"},
            format="json",
        )
        self.assertEqual(line.status_code, 201, line.data)
        adjustment = self.client.post(
            f"/api/treasury/reconciliations/{recon.data['id']}/record-adjustment/",
            {"line_id": line.data["id"], "counter_account": self.revenue_gl.pk,
             "fiscal_year": self.year.pk},
            format="json",
        )
        self.assertEqual(adjustment.status_code, 201, adjustment.data)
        self.assertEqual(adjustment.data["transaction"]["kind"], "receipt")
        self.assertEqual(adjustment.data["transaction"]["amount"], "8.0000")

        foreign_gl = ChartOfAccount.objects.create(
            company=self.company, code="1198", name_ar="بنك أجنبي للتسوية",
            nature=ChartOfAccount.Nature.ASSET,
        )
        foreign_bank = TreasuryAccount.objects.create(
            company=self.company, branch=self.branch, account=foreign_gl,
            kind="bank", code="BANK-FX-ADJ", name_ar="بنك أجنبي",
            currency_code="USD",
        )
        foreign_recon = self.client.post("/api/treasury/reconciliations/", {
            "company": self.company.pk, "bank_account": foreign_bank.pk,
            "statement_number": "ST-FX-ADJ-001", "statement_date": "2026-03-31",
            "opening_balance": "0.0000", "closing_balance": "2.0000",
        }, format="json")
        self.assertEqual(foreign_recon.status_code, 201, foreign_recon.data)
        foreign_line = self.client.post(
            f"/api/treasury/reconciliations/{foreign_recon.data['id']}/add_line/",
            {"line_number": 1, "transaction_date": "2026-03-20",
             "description": "Foreign bank fee", "reference": "FX-FEE", "amount": "-2.0000"},
            format="json",
        )
        self.assertEqual(foreign_line.status_code, 201, foreign_line.data)
        rejected = self.client.post(
            f"/api/treasury/reconciliations/{foreign_recon.data['id']}/record-adjustment/",
            {"line_id": foreign_line.data["id"], "counter_account": self.revenue_gl.pk,
             "fiscal_year": self.year.pk},
            format="json",
        )
        self.assertEqual(rejected.status_code, 400, rejected.data)
        self.assertFalse(TreasuryTransaction.objects.filter(
            document_number=f"BANK-ADJ-{foreign_recon.data['id']}-{foreign_line.data['id']}"
        ).exists())

    def test_bank_reconciliation_uses_reversed_journals_consistently(self):
        original = JournalEntry.objects.create(
            company=self.company, branch=self.branch, fiscal_year=self.year,
            entry_number="MANUAL-BANK-REV-001", entry_date=date(2026, 3, 5),
            description="قيد بنكي سيتم عكسه", source_type=JournalEntry.Source.MANUAL,
            created_by=self.user,
        )
        JournalLine.objects.bulk_create([
            JournalLine(entry=original, account=self.bank_gl, description="إثبات البنك", debit=Decimal("75.0000"), credit=0),
            JournalLine(entry=original, account=self.revenue_gl, description="إثبات الإيراد", debit=0, credit=Decimal("75.0000")),
        ])
        post_journal(original.pk)
        reversed_response = self.client.post(
            f"/api/accounting/journal-entries/{original.pk}/reverse/",
            {"entry_date": "2026-03-06"}, format="json",
        )
        self.assertEqual(reversed_response.status_code, 201, reversed_response.data)
        original.refresh_from_db()
        self.assertEqual(original.status, JournalEntry.Status.REVERSED)

        recon = self.client.post("/api/treasury/reconciliations/", {
            "company": self.company.pk, "bank_account": self.bank.pk, "statement_number": "ST-REV-001",
            "statement_date": "2026-03-31", "opening_balance": "0.0000", "closing_balance": "0.0000",
        }, format="json")
        self.assertEqual(recon.status_code, 201, recon.data)
        summary = self.client.get(f"/api/treasury/reconciliations/{recon.data['id']}/summary/")
        self.assertEqual(summary.status_code, 200, summary.data)
        self.assertEqual(summary.data["book_balance"], "0.0000")
        self.assertEqual(summary.data["unexplained_difference"], "0.0000")

        completed = self.client.post(
            f"/api/treasury/reconciliations/{recon.data['id']}/reconcile/", {}, format="json"
        )
        self.assertEqual(completed.status_code, 200, completed.data)
        self.assertEqual(completed.data["status"], "reconciled")

    def test_bank_reconciliation_rejects_wrong_amount_and_unmatched_lines(self):
        payload = {
            "company": self.company.pk, "branch": self.branch.pk, "fiscal_year": self.year.pk,
            "source_account": self.bank.pk, "counter_account": self.revenue_gl.pk,
            "kind": "receipt", "document_number": "BANK-REC-002", "transaction_date": "2026-03-10",
            "amount": "100.0000", "description": "تحصيل",
        }
        created = self.client.post("/api/treasury/transactions/", payload, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        self.client.post(f"/api/treasury/transactions/{created.data['id']}/post/", {}, format="json")
        recon = self.client.post("/api/treasury/reconciliations/", {
            "company": self.company.pk, "bank_account": self.bank.pk, "statement_number": "ST-002",
            "statement_date": "2026-03-31", "opening_balance": "0.0000", "closing_balance": "110.0000",
        }, format="json")
        line = self.client.post(f"/api/treasury/reconciliations/{recon.data['id']}/add_line/", {
            "line_number": 1, "transaction_date": "2026-03-10", "amount": "110.0000",
        }, format="json")
        mismatch = self.client.post(f"/api/treasury/reconciliations/{recon.data['id']}/match/", {
            "line_id": line.data["id"], "transaction_id": created.data["id"],
        }, format="json")
        self.assertEqual(mismatch.status_code, 400)
        finalize = self.client.post(f"/api/treasury/reconciliations/{recon.data['id']}/reconcile/", {}, format="json")
        self.assertEqual(finalize.status_code, 400)


    def test_bank_match_rejects_transaction_from_unassigned_branch(self):
        shared_bank_gl = ChartOfAccount.objects.create(company=self.company, code="1120", name_ar="بنك مشترك", nature=ChartOfAccount.Nature.ASSET)
        shared_bank = TreasuryAccount.objects.create(company=self.company, branch=None, account=shared_bank_gl, kind="bank", code="BANK-SHARED", name_ar="بنك مشترك")
        payload = {
            "company": self.company.pk, "branch": self.branch.pk, "fiscal_year": self.year.pk,
            "source_account": shared_bank.pk, "counter_account": self.revenue_gl.pk,
            "kind": "receipt", "document_number": "BANK-BRANCH-001", "transaction_date": "2026-03-10",
            "amount": "50.0000", "description": "حركة فرع غير مصرح به",
        }
        created = self.client.post("/api/treasury/transactions/", payload, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        posted = self.client.post(f"/api/treasury/transactions/{created.data['id']}/post/", {}, format="json")
        self.assertEqual(posted.status_code, 200, posted.data)
        recon = self.client.post("/api/treasury/reconciliations/", {
            "company": self.company.pk, "bank_account": shared_bank.pk, "statement_number": "BRANCH-ST-001",
            "statement_date": "2026-03-31", "opening_balance": "0.0000", "closing_balance": "50.0000",
        }, format="json")
        self.assertEqual(recon.status_code, 201, recon.data)
        line = self.client.post(f"/api/treasury/reconciliations/{recon.data['id']}/add_line/", {
            "line_number": 1, "transaction_date": "2026-03-10", "amount": "50.0000",
        }, format="json")
        self.assertEqual(line.status_code, 201, line.data)
        other_branch = Branch.objects.create(company=self.company, code="OTHER", name_ar="فرع آخر")
        limited = get_user_model().objects.create_user(username="treasury-limited", password="test-password")
        membership = CompanyMembership.objects.create(user=limited, company=self.company, role="accountant", all_branches=False)
        membership.branches.add(other_branch)
        self.client.force_authenticate(limited)
        matched = self.client.post(f"/api/treasury/reconciliations/{recon.data['id']}/match/", {
            "line_id": line.data["id"], "transaction_id": created.data["id"],
        }, format="json")
        self.assertEqual(matched.status_code, 403, matched.data)
        # The company membership does not grant visibility into a shared bank account
        # when the user lacks access to every branch; do not leak statement details.
        summary = self.client.get(f"/api/treasury/reconciliations/{recon.data['id']}/summary/")
        self.assertEqual(summary.status_code, 403, summary.data)

    def test_bank_statement_csv_import_creates_lines_without_overwriting(self):
        recon = self.client.post("/api/treasury/reconciliations/", {
            "company": self.company.pk, "bank_account": self.bank.pk, "statement_number": "CSV-001",
            "statement_date": "2026-03-31", "opening_balance": "100.0000", "closing_balance": "75.0000",
        }, format="json")
        self.assertEqual(recon.status_code, 201, recon.data)
        csv_file = SimpleUploadedFile(
            "statement.csv",
            b"date,amount,description,reference\n2026-03-02,50.00,Deposit,D-1\n2026-03-03,-75.00,Withdrawal,W-1\n",
            content_type="text/csv",
        )
        imported = self.client.post(f"/api/treasury/reconciliations/{recon.data['id']}/import_csv/", {"file": csv_file}, format="multipart")
        self.assertEqual(imported.status_code, 201, imported.data)
        self.assertEqual(imported.data["imported"], 2)
        self.assertEqual(BankStatementLine.objects.filter(reconciliation_id=recon.data["id"]).count(), 2)
        second = SimpleUploadedFile("again.csv", b"date,amount\n2026-03-04,1\n", content_type="text/csv")
        rejected = self.client.post(f"/api/treasury/reconciliations/{recon.data['id']}/import_csv/", {"file": second}, format="multipart")
        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(BankStatementLine.objects.filter(reconciliation_id=recon.data["id"]).count(), 2)

    def test_bank_statement_csv_import_rejects_invalid_rows_atomically(self):
        recon = self.client.post("/api/treasury/reconciliations/", {
            "company": self.company.pk, "bank_account": self.bank.pk, "statement_number": "CSV-002",
            "statement_date": "2026-03-31", "opening_balance": "0.0000", "closing_balance": "10.0000",
        }, format="json")
        csv_file = SimpleUploadedFile(
            "invalid.csv",
            b"date,amount\n2026-03-02,10.00\nnot-a-date,5.00\n",
            content_type="text/csv",
        )
        imported = self.client.post(f"/api/treasury/reconciliations/{recon.data['id']}/import_csv/", {"file": csv_file}, format="multipart")
        self.assertEqual(imported.status_code, 400)
        self.assertEqual(BankStatementLine.objects.filter(reconciliation_id=recon.data["id"]).count(), 0)


    def test_bank_statement_preview_and_confirm_skip_existing_and_in_file_duplicates(self):
        recon = self.client.post("/api/treasury/reconciliations/", {
            "company": self.company.pk, "bank_account": self.bank.pk, "statement_number": "PREVIEW-001",
            "statement_date": "2026-03-31", "opening_balance": "0.0000", "closing_balance": "15.0000",
        }, format="json")
        self.assertEqual(recon.status_code, 201, recon.data)
        payload = b"date,amount,description,reference\n2026-03-02,10.00,Deposit,REF-1\n2026-03-02,10.00,Deposit,REF-1\n2026-03-03,5.00,Fee,REF-2\n"
        preview = self.client.post(
            f"/api/treasury/reconciliations/{recon.data['id']}/preview-import/",
            {"file": SimpleUploadedFile("statement.csv", payload, content_type="text/csv")},
            format="multipart",
        )
        self.assertEqual(preview.status_code, 200, preview.data)
        self.assertEqual(preview.data["total"], 3)
        self.assertEqual(preview.data["new_count"], 2)
        self.assertEqual(preview.data["duplicate_count"], 1)

        confirmed = self.client.post(
            f"/api/treasury/reconciliations/{recon.data['id']}/confirm-import/",
            {"file": SimpleUploadedFile("statement.csv", payload, content_type="text/csv")},
            format="multipart",
        )
        self.assertEqual(confirmed.status_code, 201, confirmed.data)
        self.assertEqual(confirmed.data["imported"], 2)
        self.assertEqual(confirmed.data["duplicate_count"], 1)
        self.assertEqual(BankStatementLine.objects.filter(reconciliation_id=recon.data["id"]).count(), 2)

        repeated = self.client.post(
            f"/api/treasury/reconciliations/{recon.data['id']}/confirm-import/",
            {"file": SimpleUploadedFile("statement.csv", payload, content_type="text/csv")},
            format="multipart",
        )
        self.assertEqual(repeated.status_code, 200, repeated.data)
        self.assertEqual(repeated.data["imported"], 0)
        self.assertEqual(BankStatementLine.objects.filter(reconciliation_id=recon.data["id"]).count(), 2)

        next_recon = self.client.post("/api/treasury/reconciliations/", {
            "company": self.company.pk, "bank_account": self.bank.pk, "statement_number": "PREVIEW-002",
            "statement_date": "2026-03-31", "opening_balance": "15.0000", "closing_balance": "15.0000",
        }, format="json")
        previous_statement = self.client.post(
            f"/api/treasury/reconciliations/{next_recon.data['id']}/preview-import/",
            {"file": SimpleUploadedFile("statement.csv", payload, content_type="text/csv")},
            format="multipart",
        )
        self.assertEqual(previous_statement.status_code, 200, previous_statement.data)
        self.assertEqual(previous_statement.data["new_count"], 0)
        self.assertEqual(previous_statement.data["duplicate_count"], 3)

    def test_bank_statement_xlsx_import_preview(self):
        recon = self.client.post("/api/treasury/reconciliations/", {
            "company": self.company.pk, "bank_account": self.bank.pk, "statement_number": "XLSX-001",
            "statement_date": "2026-03-31", "opening_balance": "0.0000", "closing_balance": "25.5000",
        }, format="json")
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["date", "amount", "description", "reference"])
        sheet.append([date(2026, 3, 2), 25.5, "Excel deposit", "XL-1"])
        stream = BytesIO()
        workbook.save(stream)
        preview = self.client.post(
            f"/api/treasury/reconciliations/{recon.data['id']}/preview-import/",
            {"file": SimpleUploadedFile("statement.xlsx", stream.getvalue(), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
            format="multipart",
        )
        self.assertEqual(preview.status_code, 200, preview.data)
        self.assertEqual(preview.data["new_count"], 1)
        self.assertEqual(preview.data["rows"][0]["reference"], "XL-1")

    def test_bank_statement_csv_supports_semicolon_and_decimal_comma(self):
        recon = self.client.post("/api/treasury/reconciliations/", {
            "company": self.company.pk, "bank_account": self.bank.pk, "statement_number": "CSV-COMMA-001",
            "statement_date": "2026-03-31", "opening_balance": "0.0000", "closing_balance": "12.7500",
        }, format="json")
        payload = "date;amount;description;reference\n2026-03-04;12,75;Deposit;REF-COMMA\n".encode()
        preview = self.client.post(
            f"/api/treasury/reconciliations/{recon.data['id']}/preview-import/",
            {"file": SimpleUploadedFile("statement.csv", payload, content_type="text/csv")},
            format="multipart",
        )
        self.assertEqual(preview.status_code, 200, preview.data)
        self.assertEqual(Decimal(preview.data["rows"][0]["amount"]), Decimal("12.75"))

    def test_bank_statement_ofx_and_mt940_import_preview(self):
        recon = self.client.post("/api/treasury/reconciliations/", {
            "company": self.company.pk, "bank_account": self.bank.pk, "statement_number": "TEXT-001",
            "statement_date": "2026-03-31", "opening_balance": "0.0000", "closing_balance": "125.5000",
        }, format="json")
        ofx = b"""OFXHEADER:100\nDATA:OFXSGML\nVERSION:102\n<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS><BANKTRANLIST><STMTTRN><TRNTYPE>DEP\n<DTPOSTED>20260302120000\n<TRNAMT>125.50\n<FITID>OFX-1\n<NAME>Deposit\n</STMTTRN></BANKTRANLIST></STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>"""
        ofx_preview = self.client.post(
            f"/api/treasury/reconciliations/{recon.data['id']}/preview-import/",
            {"file": SimpleUploadedFile("statement.ofx", ofx, content_type="application/octet-stream")},
            format="multipart",
        )
        self.assertEqual(ofx_preview.status_code, 200, ofx_preview.data)
        self.assertEqual(ofx_preview.data["rows"][0]["reference"], "OFX-1")
        mt940 = b":20:STATEMENT\n:25:ACCOUNT\n:60F:C260301EGP0,00\n:61:260302C125,50NTRFNONREF//MT-1\n:86:Bank deposit\n:62F:C260331EGP125,50\n"
        mt_preview = self.client.post(
            f"/api/treasury/reconciliations/{recon.data['id']}/preview-import/",
            {"file": SimpleUploadedFile("statement.mt940", mt940, content_type="text/plain")},
            format="multipart",
        )
        self.assertEqual(mt_preview.status_code, 200, mt_preview.data)
        self.assertEqual(Decimal(mt_preview.data["rows"][0]["amount"]), Decimal("125.50"))
        self.assertEqual(mt_preview.data["rows"][0]["reference"], "MT-1")

    def test_treasury_payment_over_limit_requires_manager_approval(self):
        DiscountApprovalPolicy.objects.create(
            company=self.company, max_payment_amount=Decimal("100"),
            created_by=self.user, updated_by=self.user,
        )
        seed = self._create(kind="receipt", document_number="TR-APPROVAL-SEED")
        self.assertEqual(seed.status_code, 201, seed.data)
        seeded = self.client.post(
            f"/api/treasury/transactions/{seed.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(seeded.status_code, 200, seeded.data)

        payment = self._create(
            kind="payment", document_number="TR-APPROVAL-PAY",
            amount="150", counter_account=self.revenue_gl.pk,
        )
        self.assertEqual(payment.status_code, 201, payment.data)
        request = FinancialApprovalRequest.objects.get(treasury_transaction_id=payment.data["id"])
        blocked = self.client.post(
            f"/api/treasury/transactions/{payment.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(blocked.status_code, 400, blocked.data)

        manager = get_user_model().objects.create_user(username="treasury-approval-admin", password="test-password")
        CompanyMembership.objects.create(user=manager, company=self.company, role="admin", all_branches=True)
        self.client.force_authenticate(manager)
        approved = self.client.post(
            f"/api/approvals/financial-requests/{request.pk}/approve/",
            {"note": "اعتماد سند صرف خزينة"}, format="json",
        )
        self.assertEqual(approved.status_code, 200, approved.data)
        self.client.force_authenticate(self.user)
        posted = self.client.post(
            f"/api/treasury/transactions/{payment.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(posted.status_code, 200, posted.data)


    def test_editing_high_value_payment_replaces_old_approval_snapshot(self):
        DiscountApprovalPolicy.objects.create(
            company=self.company, max_payment_amount=Decimal("100"),
            created_by=self.user, updated_by=self.user,
        )
        seed = self._create(kind="receipt", document_number="TR-SNAPSHOT-SEED")
        self.assertEqual(seed.status_code, 201, seed.data)
        self.assertEqual(self.client.post(
            f"/api/treasury/transactions/{seed.data['id']}/post/", {}, format="json"
        ).status_code, 200)
        payment = self._create(
            kind="payment", document_number="TR-SNAPSHOT-PAY", amount="150",
        )
        self.assertEqual(payment.status_code, 201, payment.data)
        old_request = FinancialApprovalRequest.objects.get(treasury_transaction_id=payment.data["id"])
        revised = self.client.patch(
            f"/api/treasury/transactions/{payment.data['id']}/",
            {"amount": "160.0000"}, format="json",
        )
        self.assertEqual(revised.status_code, 200, revised.data)
        old_request.refresh_from_db()
        self.assertEqual(old_request.status, FinancialApprovalRequest.Status.SUPERSEDED)
        current = FinancialApprovalRequest.objects.filter(
            treasury_transaction_id=payment.data["id"],
            status=FinancialApprovalRequest.Status.PENDING,
        ).get()
        self.assertEqual(current.requested_amount, Decimal("160.0000"))


    def test_treasury_payment_creator_cannot_approve_own_financial_request(self):
        creator = get_user_model().objects.create_user(
            username="treasury-payment-creator-admin", password="test-password"
        )
        CompanyMembership.objects.create(
            user=creator, company=self.company, role="admin", all_branches=True,
        )
        DiscountApprovalPolicy.objects.create(
            company=self.company, max_payment_amount=Decimal("100"),
            created_by=creator, updated_by=creator,
        )
        self.client.force_authenticate(creator)
        payment = self._create(
            kind="payment", document_number="TR-SELF-APPROVAL",
            amount="150.0000",
        )
        self.assertEqual(payment.status_code, 201, payment.data)
        approval = FinancialApprovalRequest.objects.get(
            treasury_transaction_id=payment.data["id"]
        )
        self.assertEqual(approval.requested_by_id, creator.pk)

        self_approval = self.client.post(
            f"/api/approvals/financial-requests/{approval.pk}/approve/",
            {"note": "محاولة اعتماد ذاتي"}, format="json",
        )
        self.assertEqual(self_approval.status_code, 403, self_approval.data)
        approval.refresh_from_db()
        self.assertEqual(approval.status, FinancialApprovalRequest.Status.PENDING)
        self.assertIsNone(approval.decided_by_id)

        independent_manager = get_user_model().objects.create_user(
            username="treasury-independent-approver", password="test-password"
        )
        CompanyMembership.objects.create(
            user=independent_manager, company=self.company, role="admin", all_branches=True,
        )
        self.client.force_authenticate(independent_manager)
        approved = self.client.post(
            f"/api/approvals/financial-requests/{approval.pk}/approve/",
            {"note": "اعتماد من مدير مستقل"}, format="json",
        )
        self.assertEqual(approved.status_code, 200, approved.data)
        approval.refresh_from_db()
        self.assertEqual(approval.decided_by_id, independent_manager.pk)

    def _posted_bank_receipt(self, number, amount, movement_date="2026-03-10"):
        created = self.client.post("/api/treasury/transactions/", {
            "company": self.company.pk, "branch": self.branch.pk, "fiscal_year": self.year.pk,
            "source_account": self.bank.pk, "counter_account": self.revenue_gl.pk,
            "kind": "receipt", "document_number": number, "transaction_date": movement_date,
            "amount": str(amount), "description": number,
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        posted = self.client.post(
            f"/api/treasury/transactions/{created.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(posted.status_code, 200, posted.data)
        return created.data

    def _bank_reconciliation(self, number, closing):
        created = self.client.post("/api/treasury/reconciliations/", {
            "company": self.company.pk, "bank_account": self.bank.pk,
            "statement_number": number, "statement_date": "2026-03-31",
            "opening_balance": "0.0000", "closing_balance": str(closing),
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        return created.data

    def _bank_line(self, recon_id, number, amount, movement_date="2026-03-10"):
        created = self.client.post(
            f"/api/treasury/reconciliations/{recon_id}/add_line/",
            {"line_number": number, "transaction_date": movement_date, "amount": str(amount)},
            format="json",
        )
        self.assertEqual(created.status_code, 201, created.data)
        return created.data

    def _allocate(self, recon_id, line_id, transaction_id, amount):
        return self.client.post(
            f"/api/treasury/reconciliations/{recon_id}/match/",
            {"line_id": line_id, "transaction_id": transaction_id, "amount": str(amount)},
            format="json",
        )

    def test_bank_reconciliation_many_transactions_to_one_statement_line(self):
        first = self._posted_bank_receipt("BANK-GROUP-100", "100.0000")
        second = self._posted_bank_receipt("BANK-GROUP-150", "150.0000")
        recon = self._bank_reconciliation("ST-GROUP-250", "250.0000")
        line = self._bank_line(recon["id"], 1, "250.0000")

        one = self._allocate(recon["id"], line["id"], first["id"], "100.0000")
        self.assertEqual(one.status_code, 200, one.data)
        two = self._allocate(recon["id"], line["id"], second["id"], "150.0000")
        self.assertEqual(two.status_code, 200, two.data)
        self.assertEqual(len(two.data["allocations"]), 2)
        self.assertEqual(two.data["remaining_amount"], "0.0000")
        summary = self.client.get(
            f"/api/treasury/reconciliations/{recon['id']}/summary/"
        )
        self.assertEqual(summary.status_code, 200, summary.data)
        self.assertEqual(summary.data["matched_lines"], 1)
        self.assertEqual(summary.data["unmatched_lines"], 0)
        self.assertEqual(summary.data["unexplained_difference"], "0.0000")
        completed = self.client.post(
            f"/api/treasury/reconciliations/{recon['id']}/reconcile/", {}, format="json"
        )
        self.assertEqual(completed.status_code, 200, completed.data)
        self.assertEqual(completed.data["status"], "reconciled")

    def test_bank_reconciliation_one_transaction_to_multiple_statement_lines(self):
        transaction = self._posted_bank_receipt("BANK-SPLIT-250", "250.0000")
        recon = self._bank_reconciliation("ST-SPLIT-250", "250.0000")
        first_line = self._bank_line(recon["id"], 1, "100.0000")
        second_line = self._bank_line(recon["id"], 2, "150.0000")

        first = self._allocate(recon["id"], first_line["id"], transaction["id"], "100.0000")
        self.assertEqual(first.status_code, 200, first.data)
        second = self._allocate(recon["id"], second_line["id"], transaction["id"], "150.0000")
        self.assertEqual(second.status_code, 200, second.data)
        self.assertEqual(second.data["remaining_amount"], "0.0000")
        summary = self.client.get(
            f"/api/treasury/reconciliations/{recon['id']}/summary/"
        )
        self.assertEqual(summary.status_code, 200, summary.data)
        self.assertEqual(summary.data["matched_lines"], 2)
        self.assertEqual(summary.data["unmatched_lines"], 0)
        self.assertEqual(summary.data["outstanding_total"], "0.0000")
        completed = self.client.post(
            f"/api/treasury/reconciliations/{recon['id']}/reconcile/", {}, format="json"
        )
        self.assertEqual(completed.status_code, 200, completed.data)

    def test_bank_reconciliation_prevents_overallocation_and_shows_partial_balances(self):
        transaction = self._posted_bank_receipt("BANK-PARTIAL-150", "150.0000")
        recon = self._bank_reconciliation("ST-PARTIAL-200", "200.0000")
        line = self._bank_line(recon["id"], 1, "200.0000")
        second_transaction = self._posted_bank_receipt("BANK-PARTIAL-200", "200.0000")
        partial = self._allocate(recon["id"], line["id"], transaction["id"], "100.0000")
        self.assertEqual(partial.status_code, 200, partial.data)
        self.assertEqual(partial.data["matched_amount"], "100.0000")
        self.assertEqual(partial.data["remaining_amount"], "100.0000")

        over_transaction = self._allocate(recon["id"], line["id"], transaction["id"], "100.0000")
        self.assertEqual(over_transaction.status_code, 400, over_transaction.data)
        self.assertIn("المتبقي", str(over_transaction.data))
        over_line = self._allocate(recon["id"], line["id"], second_transaction["id"], "100.0001")
        self.assertEqual(over_line.status_code, 400, over_line.data)

        summary = self.client.get(
            f"/api/treasury/reconciliations/{recon['id']}/summary/"
        )
        self.assertEqual(summary.status_code, 200, summary.data)
        self.assertEqual(summary.data["unmatched_lines"], 1)
        self.assertEqual(summary.data["unmatched_statement_total"], "100.0000")
        self.assertEqual(summary.data["outstanding_total"], "250.0000")
        self.assertEqual(summary.data["unexplained_difference"], "0.0000")
        cannot_close = self.client.post(
            f"/api/treasury/reconciliations/{recon['id']}/reconcile/", {}, format="json"
        )
        self.assertEqual(cannot_close.status_code, 400, cannot_close.data)

    def test_bank_reconciliation_reports_timing_difference_without_adjusting_entry(self):
        transaction = self._posted_bank_receipt(
            "BANK-TIMING-050", "50.0000", movement_date="2026-03-10",
        )
        recon = self._bank_reconciliation("ST-TIMING-000", "0.0000")
        summary = self.client.get(
            f"/api/treasury/reconciliations/{recon['id']}/summary/"
        )
        self.assertEqual(summary.status_code, 200, summary.data)
        self.assertEqual(summary.data["outstanding_total"], "50.0000")
        outstanding = summary.data["outstanding_transactions"]
        self.assertEqual(len(outstanding), 1)
        self.assertEqual(outstanding[0]["id"], transaction["id"])
        self.assertEqual(outstanding[0]["timing_difference_days"], 21)
        self.assertEqual(summary.data["unexplained_difference"], "0.0000")
        self.assertEqual(JournalEntry.objects.count(), 1)



class TreasuryChequeWorkflowTests(APITestCase):
    def setUp(self):
        currency = Currency.objects.create(code="EGP", name_ar="جنيه مصري")
        self.company = Company.objects.create(code="CHQ", name_ar="شركة الشيكات", country="EG", activity="commercial", base_currency=currency)
        self.branch = Branch.objects.create(company=self.company, code="MAIN", name_ar="الرئيسي", is_head_office=True)
        self.year = FiscalYear.objects.create(company=self.company, name="2026", start_date=date(2026, 1, 1), end_date=date(2026, 12, 31))
        self.user = get_user_model().objects.create_user(username="cheque-user", password="test-password")
        CompanyMembership.objects.create(user=self.user, company=self.company, role="accountant", all_branches=True)
        self.client.force_authenticate(self.user)
        self.bank_gl = ChartOfAccount.objects.create(company=self.company, code="1110", name_ar="البنك", nature=ChartOfAccount.Nature.ASSET)
        self.counter_gl = ChartOfAccount.objects.create(company=self.company, code="4100", name_ar="حساب مقابل", nature=ChartOfAccount.Nature.REVENUE)
        self.bank = TreasuryAccount.objects.create(company=self.company, branch=self.branch, account=self.bank_gl, kind="bank", code="BANK-CHQ", name_ar="البنك الرئيسي")

    def test_branch_limited_user_cannot_move_cheque_to_an_inaccessible_branch(self):
        created = self._create_incoming("MOVE-BRANCH-001")
        self.assertEqual(created.status_code, 201, created.data)
        other_branch = Branch.objects.create(
            company=self.company, code="BR2", name_ar="فرع غير متاح",
        )
        other_gl = ChartOfAccount.objects.create(
            company=self.company, code="1112", name_ar="بنك الفرع الثاني",
            nature=ChartOfAccount.Nature.ASSET,
        )
        other_bank = TreasuryAccount.objects.create(
            company=self.company, branch=other_branch, account=other_gl,
            kind="bank", code="BANK-CHQ-2", name_ar="بنك الفرع الثاني",
        )
        limited = get_user_model().objects.create_user(
            username="cheque-branch-limited", password="test-password",
        )
        membership = CompanyMembership.objects.create(
            user=limited, company=self.company, role="accountant", all_branches=False,
        )
        membership.branches.add(self.branch)
        self.client.force_authenticate(limited)

        response = self.client.patch(
            f"/api/treasury/cheques/{created.data['id']}/",
            {"branch": other_branch.pk, "bank_account": other_bank.pk},
            format="json",
        )
        self.assertEqual(response.status_code, 403, response.data)
        cheque = TreasuryCheque.objects.get(pk=created.data["id"])
        self.assertEqual(cheque.branch_id, self.branch.pk)
        self.assertEqual(cheque.bank_account_id, self.bank.pk)

    def test_cheque_cannot_be_moved_to_another_company(self):
        created = self._create_incoming("MOVE-COMPANY-001")
        self.assertEqual(created.status_code, 201, created.data)
        other_company = Company.objects.create(
            code="CHQ2", name_ar="شركة أخرى", country="EG",
            activity="commercial", base_currency=self.company.base_currency,
        )
        other_branch = Branch.objects.create(
            company=other_company, code="MAIN", name_ar="الرئيسي",
        )
        other_year = FiscalYear.objects.create(
            company=other_company, name="2026",
            start_date=date(2026, 1, 1), end_date=date(2026, 12, 31),
        )
        other_gl = ChartOfAccount.objects.create(
            company=other_company, code="1110", name_ar="بنك آخر",
            nature=ChartOfAccount.Nature.ASSET,
        )
        other_bank = TreasuryAccount.objects.create(
            company=other_company, branch=other_branch, account=other_gl,
            kind="bank", code="BANK-OTHER", name_ar="بنك آخر",
        )
        response = self.client.patch(
            f"/api/treasury/cheques/{created.data['id']}/",
            {
                "company": other_company.pk, "branch": other_branch.pk,
                "fiscal_year": other_year.pk, "bank_account": other_bank.pk,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400, response.data)
        cheque = TreasuryCheque.objects.get(pk=created.data["id"])
        self.assertEqual(cheque.company_id, self.company.pk)

    def test_branch_limited_user_cannot_create_cheque_using_company_wide_bank_account(self):
        global_gl = ChartOfAccount.objects.create(
            company=self.company, code="1113", name_ar="بنك عام",
            nature=ChartOfAccount.Nature.ASSET,
        )
        global_bank = TreasuryAccount.objects.create(
            company=self.company, branch=None, account=global_gl,
            kind="bank", code="BANK-GLOBAL-CHQ", name_ar="بنك عام",
        )
        Branch.objects.create(
            company=self.company, code="BR2", name_ar="فرع آخر",
        )
        limited = get_user_model().objects.create_user(
            username="cheque-global-bank-limited", password="test-password",
        )
        membership = CompanyMembership.objects.create(
            user=limited, company=self.company, role="accountant", all_branches=False,
        )
        membership.branches.add(self.branch)
        self.client.force_authenticate(limited)
        response = self.client.post("/api/treasury/cheques/", {
            "company": self.company.pk, "branch": self.branch.pk, "fiscal_year": self.year.pk,
            "bank_account": global_bank.pk, "direction": "incoming", "cheque_number": "GLOBAL-001",
            "counterparty": "عميل", "amount": "100",
            "issue_date": "2026-03-01", "due_date": "2026-03-15",
        }, format="json")
        self.assertEqual(response.status_code, 403, response.data)
        self.assertFalse(TreasuryCheque.objects.filter(cheque_number="GLOBAL-001").exists())

    def _create_incoming(self, number="IN-001"):
        return self.client.post("/api/treasury/cheques/", {
            "company": self.company.pk, "branch": self.branch.pk, "fiscal_year": self.year.pk,
            "bank_account": self.bank.pk, "direction": "incoming", "cheque_number": number,
            "counterparty": "عميل تجريبي", "amount": "1250.0000",
            "issue_date": "2026-03-01", "due_date": "2026-03-15",
        }, format="json")

    def _posted_receipt(self, movement_date='2026-03-15'):
        doc = self.client.post("/api/treasury/transactions/", {
            "company": self.company.pk, "branch": self.branch.pk, "fiscal_year": self.year.pk,
            "source_account": self.bank.pk, "counter_account": self.counter_gl.pk,
            "kind": "receipt", "document_number": "CHQ-CLEAR-001",
            "transaction_date": movement_date, "amount": "1250.0000", "description": "تحصيل شيك",
        }, format="json")
        self.assertEqual(doc.status_code, 201, doc.data)
        posted = self.client.post(f"/api/treasury/transactions/{doc.data['id']}/post/", {}, format="json")
        self.assertEqual(posted.status_code, 200, posted.data)
        return doc.data["id"]

    def test_incoming_cheque_moves_received_deposited_under_collection_and_cleared(self):
        created = self._create_incoming()
        self.assertEqual(created.status_code, 201, created.data)
        self.assertEqual(created.data["status"], "received")
        cheque_id = created.data["id"]
        deposited = self.client.post(f"/api/treasury/cheques/{cheque_id}/deposit/", {"deposit_date": "2026-03-10"}, format="json")
        self.assertEqual(deposited.status_code, 200, deposited.data)
        under_collection = self.client.post(f"/api/treasury/cheques/{cheque_id}/under_collection/", {}, format="json")
        self.assertEqual(under_collection.status_code, 200, under_collection.data)
        self.assertEqual(under_collection.data["status"], "under_collection")
        transaction_id = self._posted_receipt()
        cleared = self.client.post(f"/api/treasury/cheques/{cheque_id}/clear/", {
            "treasury_transaction": transaction_id, "clearing_date": "2026-03-15",
        }, format="json")
        self.assertEqual(cleared.status_code, 200, cleared.data)
        self.assertEqual(cleared.data["status"], "cleared")
        self.assertEqual(cleared.data["treasury_transaction"], transaction_id)
        duplicate = self.client.post(f"/api/treasury/cheques/{cheque_id}/clear/", {
            "treasury_transaction": transaction_id, "clearing_date": "2026-03-16",
        }, format="json")
        self.assertEqual(duplicate.status_code, 400, duplicate.data)

    def test_incoming_cheque_can_bounce_before_clearing_without_posting_cash(self):
        created = self._create_incoming("IN-BOUNCE")
        self.assertEqual(created.status_code, 201, created.data)
        bounced = self.client.post(f"/api/treasury/cheques/{created.data['id']}/bounce/", {
            "bounce_date": "2026-03-12", "reason": "رصيد غير كافٍ",
        }, format="json")
        self.assertEqual(bounced.status_code, 200, bounced.data)
        self.assertEqual(bounced.data["status"], "bounced")
        self.assertIsNone(bounced.data["treasury_transaction"])
        self.assertEqual(JournalEntry.objects.count(), 0)

    def test_cheque_rejects_duplicate_number_for_same_bank_and_direction(self):
        first = self._create_incoming("DUP-100")
        self.assertEqual(first.status_code, 201, first.data)
        second = self._create_incoming("DUP-100")
        self.assertEqual(second.status_code, 400, second.data)

    def test_outgoing_cheque_requires_matching_posted_bank_payment(self):
        self._posted_receipt(movement_date='2026-03-01')
        missing = self.client.post("/api/treasury/cheques/", {
            "company": self.company.pk, "branch": self.branch.pk, "fiscal_year": self.year.pk,
            "bank_account": self.bank.pk, "direction": "outgoing", "cheque_number": "OUT-001",
            "counterparty": "مورد تجريبي", "amount": "500.0000",
            "issue_date": "2026-03-01", "due_date": "2026-03-15",
        }, format="json")
        self.assertEqual(missing.status_code, 400, missing.data)
        doc = self.client.post("/api/treasury/transactions/", {
            "company": self.company.pk, "branch": self.branch.pk, "fiscal_year": self.year.pk,
            "source_account": self.bank.pk, "counter_account": self.counter_gl.pk,
            "kind": "payment", "document_number": "CHQ-PAY-001",
            "transaction_date": "2026-03-01", "amount": "500.0000", "description": "صرف شيك",
        }, format="json")
        self.assertEqual(doc.status_code, 201, doc.data)
        posted = self.client.post(f"/api/treasury/transactions/{doc.data['id']}/post/", {}, format="json")
        self.assertEqual(posted.status_code, 200, posted.data)
        created = self.client.post("/api/treasury/cheques/", {
            "company": self.company.pk, "branch": self.branch.pk, "fiscal_year": self.year.pk,
            "bank_account": self.bank.pk, "direction": "outgoing", "cheque_number": "OUT-001",
            "counterparty": "مورد تجريبي", "amount": "500.0000",
            "issue_date": "2026-03-01", "due_date": "2026-03-15",
            "treasury_transaction": doc.data["id"],
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        self.assertEqual(created.data["status"], "issued")
        presented = self.client.post(f"/api/treasury/cheques/{created.data['id']}/present/", {}, format="json")
        self.assertEqual(presented.status_code, 200, presented.data)
class TreasuryLiquiditySummaryTests(APITestCase):
    def setUp(self):
        currency = Currency.objects.create(code="EGP", name_ar="جنيه مصري")
        self.company = Company.objects.create(
            code="LIQ", name_ar="شركة السيولة", country="EG",
            activity="commercial", base_currency=currency,
        )
        self.branch = Branch.objects.create(
            company=self.company, code="MAIN", name_ar="الرئيسي", is_head_office=True,
        )
        self.year = FiscalYear.objects.create(
            company=self.company, name="2026",
            start_date=date(2026, 1, 1), end_date=date(2026, 12, 31),
        )
        self.user = get_user_model().objects.create_user(
            username="liquidity-user", password="test-password",
        )
        CompanyMembership.objects.create(
            user=self.user, company=self.company, role="accountant", all_branches=True,
        )
        self.client.force_authenticate(self.user)
        self.cash_gl = ChartOfAccount.objects.create(
            company=self.company, code="1100", name_ar="الصندوق",
            nature=ChartOfAccount.Nature.ASSET,
        )
        self.revenue_gl = ChartOfAccount.objects.create(
            company=self.company, code="4100", name_ar="إيرادات",
            nature=ChartOfAccount.Nature.REVENUE,
        )
        self.cash = TreasuryAccount.objects.create(
            company=self.company, branch=self.branch, account=self.cash_gl,
            kind="cash", code="CASH-LIQ", name_ar="الخزينة",
        )

    def test_liquidity_summary_counts_posted_ledger_only_and_excludes_closing_entries(self):
        posted = JournalEntry.objects.create(
            company=self.company, branch=self.branch, fiscal_year=self.year,
            entry_number="LIQ-POSTED", entry_date=date(2026, 3, 1),
            description="رصيد نقدي مرحل",
        )
        JournalLine.objects.create(
            entry=posted, account=self.cash_gl, debit=Decimal("1250"), credit=0,
        )
        JournalLine.objects.create(
            entry=posted, account=self.revenue_gl, debit=0, credit=Decimal("1250"),
        )
        post_journal(posted.pk, actor=self.user)

        draft = JournalEntry.objects.create(
            company=self.company, branch=self.branch, fiscal_year=self.year,
            entry_number="LIQ-DRAFT", entry_date=date(2026, 3, 2),
            description="مسودة لا تدخل الرصيد",
        )
        JournalLine.objects.create(
            entry=draft, account=self.cash_gl, debit=Decimal("900"), credit=0,
        )
        JournalLine.objects.create(
            entry=draft, account=self.revenue_gl, debit=0, credit=Decimal("900"),
        )

        response = self.client.get(
            f"/api/treasury/accounts/liquidity-summary/?company={self.company.pk}"
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(Decimal(response.data["total_base_balance"]), Decimal("1250"))
        self.assertEqual(Decimal(response.data["accounts"][0]["base_balance"]), Decimal("1250"))

    def test_liquidity_summary_keeps_foreign_nominal_and_base_carrying_values_separate(self):
        Currency.objects.create(code="USD", name_ar="دولار أمريكي")
        foreign_gl = ChartOfAccount.objects.create(
            company=self.company, code="1120", name_ar="بنك دولار",
            nature=ChartOfAccount.Nature.ASSET,
        )
        foreign_account = TreasuryAccount.objects.create(
            company=self.company, branch=self.branch, account=foreign_gl,
            kind="bank", code="BANK-USD-LIQ", name_ar="بنك دولار",
            currency_code="USD", opening_balance=Decimal("150000"),
            opening_foreign_amount=Decimal("5000"),
            opening_exchange_rate=Decimal("30"),
            opening_exchange_rate_date=date(2025, 12, 31),
        )
        TreasuryFxPosition.objects.create(
            account=foreign_account, foreign_amount=Decimal("5000"),
            carrying_base_amount=Decimal("150000"),
        )

        response = self.client.get(
            f"/api/treasury/accounts/liquidity-summary/?company={self.company.pk}"
        )
        self.assertEqual(response.status_code, 200, response.data)
        row = next(item for item in response.data["accounts"] if item["id"] == foreign_account.pk)
        self.assertEqual(row["currency"], "USD")
        self.assertEqual(Decimal(row["balance"]), Decimal("5000"))
        self.assertEqual(Decimal(row["base_balance"]), Decimal("150000"))


class TreasuryLiquidityForecastTests(TreasuryLiquiditySummaryTests):
    def test_forecast_uses_only_remaining_posted_receivables_payables_and_unpaid_payroll(self):
        from datetime import timedelta
        from customers.models import Customer
        from suppliers.models import Supplier
        from sales.models import SalesInvoice, CustomerReceipt
        from purchasing.models import PurchaseInvoice, SupplierPayment
        from hr.models import Employee, PayrollCycle, PayrollResult
        from django.utils import timezone

        today = timezone.localdate()
        customer = Customer.objects.create(
            company=self.company, code="C-LIQ-1", name_ar="عميل اختبار",
            payment_terms_days=5,
        )
        sales_invoice = SalesInvoice.objects.create(
            company=self.company, customer=customer, branch=self.branch,
            invoice_number="SI-LIQ-1", invoice_date=today - timedelta(days=10),
            status=SalesInvoice.Status.POSTED, grand_total=Decimal("100"),
        )
        CustomerReceipt.objects.create(
            company=self.company, customer=customer, invoice=sales_invoice,
            receipt_number="CR-LIQ-1", receipt_date=today - timedelta(days=2),
            amount=Decimal("40"), status=CustomerReceipt.Status.POSTED,
        )
        CustomerReceipt.objects.create(
            company=self.company, customer=customer, invoice=sales_invoice,
            receipt_number="CR-LIQ-2", receipt_date=today - timedelta(days=1),
            amount=Decimal("20"), status=CustomerReceipt.Status.REVERSED,
        )

        supplier = Supplier.objects.create(
            company=self.company, code="S-LIQ-1", name_ar="مورد اختبار",
            payment_terms_days=3,
        )
        purchase_invoice = PurchaseInvoice.objects.create(
            company=self.company, supplier=supplier, branch=self.branch,
            invoice_number="PI-LIQ-1", invoice_date=today - timedelta(days=8),
            status=PurchaseInvoice.Status.POSTED, grand_total=Decimal("80"),
        )
        SupplierPayment.objects.create(
            company=self.company, supplier=supplier, invoice=purchase_invoice,
            payment_number="SP-LIQ-1", payment_date=today - timedelta(days=1),
            amount=Decimal("30"), status=SupplierPayment.Status.POSTED,
        )

        employee = Employee.objects.create(
            company=self.company, branch=self.branch, code="E-LIQ-1",
            full_name_ar="موظف اختبار", hire_date=today - timedelta(days=365),
            currency=self.company.base_currency, basic_salary=Decimal("100"),
        )
        cycle = PayrollCycle.objects.create(
            company=self.company, period_start=today - timedelta(days=30),
            period_end=today - timedelta(days=1), payment_date=today + timedelta(days=10),
            status=PayrollCycle.Status.APPROVED,
        )
        PayrollResult.objects.create(
            cycle=cycle, employee=employee, basic_salary_snapshot=Decimal("100"),
            net_pay=Decimal("75"),
        )

        result = self.client.get(
            f"/api/treasury/accounts/liquidity-forecast/?company={self.company.pk}&days=30"
        )
        self.assertEqual(result.status_code, 200, result.data)
        self.assertEqual(Decimal(result.data["expected_collections"]), Decimal("60"))
        self.assertEqual(Decimal(result.data["expected_supplier_payments"]), Decimal("50"))
        self.assertEqual(Decimal(result.data["expected_payroll"]), Decimal("75"))
        self.assertEqual(Decimal(result.data["expected_payments"]), Decimal("125"))
        self.assertEqual(Decimal(result.data["expected_net"]), Decimal("-65"))
        self.assertEqual(Decimal(result.data["projected_closing_balance"]), Decimal("-65"))
        self.assertEqual(len([item for item in result.data["items"] if item["document"] == "SI-LIQ-1"]), 1)
        self.assertEqual(len([item for item in result.data["items"] if item["document"] == "PI-LIQ-1"]), 1)

    def test_forecast_rejects_unsupported_horizon_and_enforces_branch_access(self):
        invalid = self.client.get(
            f"/api/treasury/accounts/liquidity-forecast/?company={self.company.pk}&days=45"
        )
        self.assertEqual(invalid.status_code, 400, invalid.data)

        other = Branch.objects.create(
            company=self.company, code="OTHER", name_ar="فرع آخر",
        )
        restricted = get_user_model().objects.create_user(
            username="liquidity-restricted", password="test-password",
        )
        CompanyMembership.objects.create(
            user=restricted, company=self.company, role="accountant", all_branches=False,
        )
        self.client.force_authenticate(restricted)
        denied = self.client.get(
            f"/api/treasury/accounts/liquidity-forecast/?company={self.company.pk}&days=30&branch={other.pk}"
        )
        self.assertEqual(denied.status_code, 403, denied.data)


    def test_reconciled_bank_statement_can_be_reopened_with_reason_and_audit(self):
        bank_gl = ChartOfAccount.objects.create(
            company=self.company, code="1198", name_ar="بنك إعادة الفتح",
            nature=ChartOfAccount.Nature.ASSET,
        )
        bank = TreasuryAccount.objects.create(
            company=self.company, branch=self.branch, account=bank_gl,
            kind="bank", code="BANK-REOPEN-1", name_ar="بنك إعادة الفتح",
        )
        recon = BankReconciliation.objects.create(
            company=self.company, bank_account=bank,
            statement_number="ST-REOPEN-001", statement_date=date(2026, 3, 31),
            opening_balance=Decimal("0"), closing_balance=Decimal("25"),
            status=BankReconciliation.Status.RECONCILED,
            reconciled_at=timezone.now(), created_by=self.user,
        )
        transaction_response = self.client.post("/api/treasury/transactions/", {
            "company": self.company.pk, "branch": self.branch.pk, "fiscal_year": self.year.pk,
            "source_account": self.cash.pk, "counter_account": self.revenue_gl.pk,
            "kind": "receipt", "document_number": "REOPEN-KEEP-001",
            "transaction_date": "2026-03-15", "amount": "25.0000",
            "description": "حركة مرتبطة بسطر كشف البنك",
        }, format="json")
        self.assertEqual(transaction_response.status_code, 201, transaction_response.data)
        posted_transaction = self.client.post(
            f"/api/treasury/transactions/{transaction_response.data['id']}/post/",
            {}, format="json",
        )
        self.assertEqual(posted_transaction.status_code, 200, posted_transaction.data)
        statement_line = BankStatementLine.objects.create(
            reconciliation=recon, line_number=1, transaction_date=date(2026, 3, 15),
            description="تحصيل بنكي", reference="REOPEN-REF",
            amount=Decimal("25.0000"),
        )
        allocation = BankStatementMatchAllocation.objects.create(
            line=statement_line, transaction_id=posted_transaction.data["id"],
            amount=Decimal("25.0000"),
        )
        missing_reason = self.client.post(
            f"/api/treasury/reconciliations/{recon.pk}/reopen/", {}, format="json",
        )
        self.assertEqual(missing_reason.status_code, 400)
        recon.refresh_from_db()
        self.assertEqual(recon.status, BankReconciliation.Status.RECONCILED)

        response = self.client.post(
            f"/api/treasury/reconciliations/{recon.pk}/reopen/",
            {"reason": "تصحيح سطر كشف بنك تم إدخاله بقيمة غير صحيحة"},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["status"], "draft")
        self.assertIsNone(response.data["reconciled_at"])
        recon.refresh_from_db()
        self.assertEqual(recon.status, BankReconciliation.Status.DRAFT)
        self.assertIsNone(recon.reconciled_at)
        event = AuditEvent.objects.get(
            resource="treasury.reconciliations.reopen", object_id=str(recon.pk),
            succeeded=True,
        )
        self.assertEqual(event.actor_id, self.user.pk)
        self.assertEqual(
            event.metadata["reason"],
            "تصحيح سطر كشف بنك تم إدخاله بقيمة غير صحيحة",
        )
        self.assertTrue(BankStatementLine.objects.filter(pk=statement_line.pk, reconciliation=recon).exists())
        allocation.refresh_from_db()
        self.assertEqual(allocation.line_id, statement_line.pk)
        self.assertEqual(allocation.transaction_id, posted_transaction.data["id"])
        self.assertEqual(allocation.amount, Decimal("25.0000"))

    def test_reopen_is_blocked_when_a_later_bank_reconciliation_is_already_reconciled(self):
        bank_gl = ChartOfAccount.objects.create(
            company=self.company, code="1198", name_ar="بنك إعادة الفتح",
            nature=ChartOfAccount.Nature.ASSET,
        )
        bank = TreasuryAccount.objects.create(
            company=self.company, branch=self.branch, account=bank_gl,
            kind="bank", code="BANK-REOPEN-1", name_ar="بنك إعادة الفتح",
        )
        earlier = BankReconciliation.objects.create(
            company=self.company, bank_account=bank,
            statement_number="ST-REOPEN-OLD", statement_date=date(2026, 3, 31),
            opening_balance=Decimal("0"), closing_balance=Decimal("0"),
            status=BankReconciliation.Status.RECONCILED,
            reconciled_at=timezone.now(), created_by=self.user,
        )
        BankReconciliation.objects.create(
            company=self.company, bank_account=bank,
            statement_number="ST-REOPEN-NEW", statement_date=date(2026, 4, 30),
            opening_balance=Decimal("0"), closing_balance=Decimal("0"),
            status=BankReconciliation.Status.RECONCILED,
            reconciled_at=timezone.now(), created_by=self.user,
        )
        response = self.client.post(
            f"/api/treasury/reconciliations/{earlier.pk}/reopen/",
            {"reason": "مراجعة خطأ قديم في كشف البنك"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        earlier.refresh_from_db()
        self.assertEqual(earlier.status, BankReconciliation.Status.RECONCILED)


class PettyCashWorkflowTests(APITestCase):
    def setUp(self):
        currency = Currency.objects.create(code="EGP", name_ar="جنيه مصري")
        self.company = Company.objects.create(code="PC", name_ar="شركة العهدة", country="EG", activity="commercial", base_currency=currency)
        self.branch = Branch.objects.create(company=self.company, code="MAIN", name_ar="الرئيسي", is_head_office=True)
        self.year = FiscalYear.objects.create(company=self.company, name="2026", start_date=date(2026, 1, 1), end_date=date(2026, 12, 31))
        self.user = get_user_model().objects.create_user(username="petty-cash-user", password="test-password")
        self.approver = get_user_model().objects.create_user(username="petty-cash-approver", password="test-password")
        self.new_custodian = get_user_model().objects.create_user(username="petty-cash-custodian-2", password="test-password")
        CompanyMembership.objects.create(user=self.user, company=self.company, role="accountant", all_branches=True)
        CompanyMembership.objects.create(user=self.approver, company=self.company, role="accountant", all_branches=True)
        CompanyMembership.objects.create(user=self.new_custodian, company=self.company, role="accountant", all_branches=True)
        self.client.force_authenticate(self.user)
        self.cash_gl = ChartOfAccount.objects.create(company=self.company, code="1100", name_ar="الخزينة الرئيسية", nature=ChartOfAccount.Nature.ASSET)
        self.petty_gl = ChartOfAccount.objects.create(company=self.company, code="1110", name_ar="عهدة نثرية", nature=ChartOfAccount.Nature.ASSET)
        self.equity_gl = ChartOfAccount.objects.create(company=self.company, code="3000", name_ar="حقوق ملكية", nature=ChartOfAccount.Nature.EQUITY)
        self.expense_gl = ChartOfAccount.objects.create(company=self.company, code="6100", name_ar="مصروفات مكتبية", nature=ChartOfAccount.Nature.EXPENSE)
        self.cash = TreasuryAccount.objects.create(company=self.company, branch=self.branch, account=self.cash_gl, kind="cash", code="CASH-PC", name_ar="الخزينة الرئيسية")
        self.petty_account = TreasuryAccount.objects.create(company=self.company, branch=self.branch, account=self.petty_gl, kind="cash", code="PETTY-PC", name_ar="عهدة المكتب")
        opening = JournalEntry.objects.create(
            company=self.company, branch=self.branch, fiscal_year=self.year,
            entry_number="PC-OPEN-001", entry_date=date(2026, 1, 1),
            description="رصيد افتتاحي للاختبار", source_type=JournalEntry.Source.MANUAL,
            created_by=self.user,
        )
        JournalLine.objects.bulk_create([
            JournalLine(entry=opening, account=self.cash_gl, debit=Decimal("1000"), credit=0),
            JournalLine(entry=opening, account=self.equity_gl, debit=0, credit=Decimal("1000")),
        ])
        post_journal(opening.pk, actor=self.user)

    def test_branch_limited_user_cannot_move_petty_cash_fund_to_an_inaccessible_branch(self):
        fund_id = self._create_fund()
        other_branch = Branch.objects.create(
            company=self.company, code="BR2", name_ar="فرع غير متاح",
        )
        other_cash_gl = ChartOfAccount.objects.create(
            company=self.company, code="1102", name_ar="خزينة الفرع الثاني",
            nature=ChartOfAccount.Nature.ASSET,
        )
        other_petty_gl = ChartOfAccount.objects.create(
            company=self.company, code="1112", name_ar="عهدة الفرع الثاني",
            nature=ChartOfAccount.Nature.ASSET,
        )
        other_cash = TreasuryAccount.objects.create(
            company=self.company, branch=other_branch, account=other_cash_gl,
            kind="cash", code="CASH-PC-2", name_ar="خزينة الفرع الثاني",
        )
        other_petty = TreasuryAccount.objects.create(
            company=self.company, branch=other_branch, account=other_petty_gl,
            kind="cash", code="PETTY-PC-2", name_ar="عهدة الفرع الثاني",
        )
        limited = get_user_model().objects.create_user(
            username="petty-branch-limited", password="test-password",
        )
        membership = CompanyMembership.objects.create(
            user=limited, company=self.company, role="accountant", all_branches=False,
        )
        membership.branches.add(self.branch)
        self.client.force_authenticate(limited)

        response = self.client.patch(
            f"/api/treasury/petty-cash-funds/{fund_id}/",
            {
                "branch": other_branch.pk, "funding_account": other_cash.pk,
                "petty_cash_account": other_petty.pk,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 403, response.data)
        fund = PettyCashFund.objects.get(pk=fund_id)
        self.assertEqual(fund.branch_id, self.branch.pk)
        self.assertEqual(fund.funding_account_id, self.cash.pk)
        self.assertEqual(fund.petty_cash_account_id, self.petty_account.pk)

    def test_branch_limited_user_cannot_create_petty_cash_fund_with_company_wide_accounts(self):
        Branch.objects.create(
            company=self.company, code="BR2", name_ar="فرع آخر",
        )
        global_cash_gl = ChartOfAccount.objects.create(
            company=self.company, code="1102", name_ar="خزينة عامة",
            nature=ChartOfAccount.Nature.ASSET,
        )
        global_petty_gl = ChartOfAccount.objects.create(
            company=self.company, code="1112", name_ar="عهدة عامة",
            nature=ChartOfAccount.Nature.ASSET,
        )
        global_cash = TreasuryAccount.objects.create(
            company=self.company, branch=None, account=global_cash_gl,
            kind="cash", code="CASH-PC-GLOBAL", name_ar="خزينة عامة",
        )
        global_petty = TreasuryAccount.objects.create(
            company=self.company, branch=None, account=global_petty_gl,
            kind="cash", code="PETTY-PC-GLOBAL", name_ar="عهدة عامة",
        )
        limited = get_user_model().objects.create_user(
            username="petty-global-account-limited", password="test-password",
        )
        membership = CompanyMembership.objects.create(
            user=limited, company=self.company, role="accountant", all_branches=False,
        )
        membership.branches.add(self.branch)
        self.client.force_authenticate(limited)
        response = self.client.post("/api/treasury/petty-cash-funds/", {
            "company": self.company.pk, "branch": self.branch.pk, "code": "PC-GLOBAL",
            "name": "عهدة بحسابات عامة", "custodian": limited.pk,
            "funding_account": global_cash.pk, "petty_cash_account": global_petty.pk,
            "authorized_amount": "100",
        }, format="json")
        self.assertEqual(response.status_code, 403, response.data)
        self.assertFalse(PettyCashFund.objects.filter(code="PC-GLOBAL").exists())

    def _create_fund(self):
        response = self.client.post("/api/treasury/petty-cash-funds/", {
            "company": self.company.pk, "branch": self.branch.pk, "code": "PC-01",
            "name": "عهدة المكتب", "custodian": self.user.pk,
            "funding_account": self.cash.pk, "petty_cash_account": self.petty_account.pk,
            "authorized_amount": "500",
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        return response.data["id"]

    def test_imprest_fund_issue_settlement_replenishment_and_count(self):
        fund_id = self._create_fund()
        funded = self.client.post(
            f"/api/treasury/petty-cash-funds/{fund_id}/fund/",
            {"amount": "500", "transaction_date": "2026-03-01"}, format="json",
        )
        self.assertEqual(funded.status_code, 201, funded.data)
        transfer = TreasuryTransaction.objects.get(pk=funded.data["transfer_id"])
        self.assertEqual(transfer.kind, TreasuryTransaction.Kind.TRANSFER)
        self.assertEqual(transfer.status, TreasuryTransaction.Status.POSTED)

        expense = self.client.post("/api/treasury/petty-cash-expenses/", {
            "fund": fund_id, "expense_date": "2026-03-02",
            "description": "شراء أدوات مكتبية", "amount": "100",
            "expense_account": self.expense_gl.pk, "receipt_number": "R-100",
        }, format="json")
        self.assertEqual(expense.status_code, 201, expense.data)
        submitted = self.client.post(f"/api/treasury/petty-cash-expenses/{expense.data['id']}/submit/", {}, format="json")
        self.assertEqual(submitted.status_code, 200, submitted.data)
        self.client.force_authenticate(self.approver)
        approved = self.client.post(f"/api/treasury/petty-cash-expenses/{expense.data['id']}/approve/", {"approval_note": "مراجعة المستندات سليمة"}, format="json")
        self.assertEqual(approved.status_code, 200, approved.data)
        self.client.force_authenticate(self.user)

        settled = self.client.post(
            f"/api/treasury/petty-cash-funds/{fund_id}/settle/",
            {"expense_ids": [expense.data["id"]], "settlement_date": "2026-03-03"},
            format="json",
        )
        self.assertEqual(settled.status_code, 201, settled.data)
        journal = JournalEntry.objects.get(pk=settled.data["journal_entry"])
        self.assertEqual(journal.status, JournalEntry.Status.POSTED)
        self.assertEqual(journal.lines.get(account=self.expense_gl).debit, Decimal("100"))
        self.assertEqual(journal.lines.get(account=self.petty_gl).credit, Decimal("100"))
        self.assertEqual(PettyCashExpense.objects.get(pk=expense.data["id"]).status, PettyCashExpense.Status.SETTLED)

        balance = self.client.get(f"/api/treasury/petty-cash-funds/{fund_id}/balance/")
        self.assertEqual(balance.status_code, 200, balance.data)
        self.assertEqual(Decimal(str(balance.data["book_balance"])), Decimal("400"))

        replenished = self.client.post(
            f"/api/treasury/petty-cash-funds/{fund_id}/replenish/",
            {"amount": "100", "transaction_date": "2026-03-04"}, format="json",
        )
        self.assertEqual(replenished.status_code, 200, replenished.data)
        self.assertEqual(PettyCashSettlement.objects.get(pk=settled.data["settlement_id"]).replenished_amount, Decimal("100"))

        count = self.client.post(
            f"/api/treasury/petty-cash-funds/{fund_id}/count/",
            {"count_date": "2026-03-04", "actual_balance": "498", "notes": "عجز فعلي 2"},
            format="json",
        )
        self.assertEqual(count.status_code, 201, count.data)
        self.assertEqual(count.data["status"], PettyCashCount.Status.PENDING)
        self.assertEqual(Decimal(str(count.data["expected_balance"])), Decimal("500"))
        self.assertEqual(Decimal(str(count.data["variance"])), Decimal("-2"))
        self.client.force_authenticate(self.approver)
        approved_count = self.client.post(
            f"/api/treasury/petty-cash-funds/{fund_id}/approve-count/",
            {"count_id": count.data["id"], "counter_account": self.expense_gl.pk, "review_note": "تمت مراجعة العجز واعتماد المعالجة"},
            format="json",
        )
        self.assertEqual(approved_count.status_code, 200, approved_count.data)
        self.assertEqual(approved_count.data["status"], PettyCashCount.Status.APPROVED)
        self.assertIsNotNone(approved_count.data["adjustment_journal_entry"])
        adjustment = JournalEntry.objects.get(pk=approved_count.data["adjustment_journal_entry"])
        self.assertEqual(adjustment.status, JournalEntry.Status.POSTED)
        self.assertEqual(adjustment.lines.get(account=self.expense_gl).debit, Decimal("2"))
        self.client.force_authenticate(self.user)

    def test_petty_cash_settlement_cannot_exceed_fund_balance(self):
        fund_id = self._create_fund()
        expense = self.client.post("/api/treasury/petty-cash-expenses/", {
            "fund": fund_id, "expense_date": "2026-03-02",
            "description": "مصروف دون تمويل", "amount": "10",
            "expense_account": self.expense_gl.pk, "receipt_number": "R-200",
        }, format="json")
        self.assertEqual(expense.status_code, 201, expense.data)
        submitted = self.client.post(f"/api/treasury/petty-cash-expenses/{expense.data['id']}/submit/", {}, format="json")
        self.assertEqual(submitted.status_code, 200, submitted.data)
        self.client.force_authenticate(self.approver)
        approved = self.client.post(f"/api/treasury/petty-cash-expenses/{expense.data['id']}/approve/", {}, format="json")
        self.assertEqual(approved.status_code, 200, approved.data)
        self.client.force_authenticate(self.user)
        settled = self.client.post(
            f"/api/treasury/petty-cash-funds/{fund_id}/settle/",
            {"expense_ids": [expense.data["id"]], "settlement_date": "2026-03-03"},
            format="json",
        )
        self.assertEqual(settled.status_code, 400)
        self.assertEqual(PettyCashSettlement.objects.count(), 0)
        self.assertEqual(JournalEntry.objects.filter(description__contains="تسوية مصروفات العهدة").count(), 0)

    def test_expense_creator_cannot_approve_own_expense(self):
        fund_id = self._create_fund()
        expense = self.client.post("/api/treasury/petty-cash-expenses/", {
            "fund": fund_id, "expense_date": "2026-03-02", "description": "مصروف اختبار الاعتماد",
            "amount": "15", "expense_account": self.expense_gl.pk, "receipt_number": "R-300",
        }, format="json")
        self.assertEqual(expense.status_code, 201, expense.data)
        submitted = self.client.post(f"/api/treasury/petty-cash-expenses/{expense.data['id']}/submit/", {}, format="json")
        self.assertEqual(submitted.status_code, 200, submitted.data)
        self_response = self.client.post(f"/api/treasury/petty-cash-expenses/{expense.data['id']}/approve/", {}, format="json")
        self.assertEqual(self_response.status_code, 400, self_response.data)
        self.assertEqual(PettyCashExpense.objects.get(pk=expense.data["id"]).status, PettyCashExpense.Status.PENDING_APPROVAL)

    def test_custodian_handover_records_balance_and_changes_custodian(self):
        fund_id = self._create_fund()
        funded = self.client.post(
            f"/api/treasury/petty-cash-funds/{fund_id}/fund/",
            {"amount": "500", "transaction_date": "2026-03-01"}, format="json",
        )
        self.assertEqual(funded.status_code, 201, funded.data)
        handover = self.client.post(
            f"/api/treasury/petty-cash-funds/{fund_id}/handover/",
            {"new_custodian": self.new_custodian.pk, "handover_date": "2026-03-05",
             "actual_balance": "500", "notes": "تسليم العهدة بمحضر موقع"},
            format="json",
        )
        self.assertEqual(handover.status_code, 201, handover.data)
        fund = PettyCashFund.objects.get(pk=fund_id)
        self.assertEqual(fund.custodian_id, self.new_custodian.pk)
        self.assertEqual(PettyCashCustodianHandover.objects.get(pk=handover.data["id"]).previous_custodian_id, self.user.pk)

    def test_close_requires_physical_balance_confirmation(self):
        fund_id = self._create_fund()
        self.client.post(
            f"/api/treasury/petty-cash-funds/{fund_id}/fund/",
            {"amount": "500", "transaction_date": "2026-03-01"}, format="json",
        )
        expense = self.client.post("/api/treasury/petty-cash-expenses/", {
            "fund": fund_id, "expense_date": "2026-03-02", "description": "مصروف قبل الإغلاق",
            "amount": "100", "expense_account": self.expense_gl.pk, "receipt_number": "R-400",
        }, format="json")
        self.assertEqual(expense.status_code, 201, expense.data)
        self.client.post(f"/api/treasury/petty-cash-expenses/{expense.data['id']}/submit/", {}, format="json")
        self.client.force_authenticate(self.approver)
        self.client.post(f"/api/treasury/petty-cash-expenses/{expense.data['id']}/approve/", {}, format="json")
        self.client.force_authenticate(self.user)
        settled = self.client.post(
            f"/api/treasury/petty-cash-funds/{fund_id}/settle/",
            {"expense_ids": [expense.data["id"]], "settlement_date": "2026-03-03"}, format="json",
        )
        self.assertEqual(settled.status_code, 201, settled.data)
        closed = self.client.post(f"/api/treasury/petty-cash-funds/{fund_id}/close/", {}, format="json")
        self.assertEqual(closed.status_code, 400, closed.data)
        self.assertEqual(PettyCashFund.objects.get(pk=fund_id).status, PettyCashFund.Status.ACTIVE)

    def test_close_returns_remaining_cash_to_funding_account_and_closes_fund(self):
        fund_id = self._create_fund()
        funded = self.client.post(
            f"/api/treasury/petty-cash-funds/{fund_id}/fund/",
            {"amount": "500", "transaction_date": "2026-03-01"}, format="json",
        )
        self.assertEqual(funded.status_code, 201, funded.data)
        expense = self.client.post("/api/treasury/petty-cash-expenses/", {
            "fund": fund_id, "expense_date": "2026-03-02", "description": "مصروف نهائي قبل الإغلاق",
            "amount": "100", "expense_account": self.expense_gl.pk, "receipt_number": "R-500",
        }, format="json")
        self.assertEqual(expense.status_code, 201, expense.data)
        self.client.post(f"/api/treasury/petty-cash-expenses/{expense.data['id']}/submit/", {}, format="json")
        self.client.force_authenticate(self.approver)
        self.client.post(f"/api/treasury/petty-cash-expenses/{expense.data['id']}/approve/", {}, format="json")
        self.client.force_authenticate(self.user)
        settled = self.client.post(
            f"/api/treasury/petty-cash-funds/{fund_id}/settle/",
            {"expense_ids": [expense.data["id"]], "settlement_date": "2026-03-03"}, format="json",
        )
        self.assertEqual(settled.status_code, 201, settled.data)
        closed = self.client.post(
            f"/api/treasury/petty-cash-funds/{fund_id}/close/",
            {"actual_balance": "400"}, format="json",
        )
        self.assertEqual(closed.status_code, 200, closed.data)
        self.assertEqual(Decimal(str(closed.data["returned_amount"])), Decimal("400.0000"))
        fund = PettyCashFund.objects.get(pk=fund_id)
        self.assertEqual(fund.status, PettyCashFund.Status.CLOSED)
        balance = self.client.get(f"/api/treasury/petty-cash-funds/{fund_id}/balance/")
        self.assertEqual(Decimal(str(balance.data["book_balance"])), Decimal("0"))
        return_transfer = PettyCashTransfer.objects.get(fund=fund, kind=PettyCashTransfer.Kind.CLOSURE_RETURN)
        self.assertEqual(return_transfer.amount, Decimal("400"))
        self.assertEqual(return_transfer.treasury_transaction.status, TreasuryTransaction.Status.POSTED)


    def test_receipt_attachment_upload_and_authorized_download(self):
        fund_id = self._create_fund()
        upload = SimpleUploadedFile(
            "receipt.pdf", b"%PDF-1.4\n% petty cash receipt test\n%%EOF",
            content_type="application/pdf",
        )
        expense = self.client.post("/api/treasury/petty-cash-expenses/", {
            "fund": fund_id, "expense_date": "2026-03-02", "description": "مصروف بمرفق",
            "amount": "20", "expense_account": self.expense_gl.pk, "receipt_upload": upload,
        }, format="multipart")
        self.assertEqual(expense.status_code, 201, expense.data)
        self.assertTrue(PettyCashExpense.objects.get(pk=expense.data["id"]).receipt_file_data)
        submitted = self.client.post(f"/api/treasury/petty-cash-expenses/{expense.data['id']}/submit/", {}, format="json")
        self.assertEqual(submitted.status_code, 200, submitted.data)
        download = self.client.get(f"/api/treasury/petty-cash-expenses/{expense.data['id']}/receipt/")
        self.assertEqual(download.status_code, 200)
        self.assertIn(b"%PDF-1.4", b"".join(download.streaming_content))

    def test_receipt_attachment_rejects_unsupported_file_type(self):
        fund_id = self._create_fund()
        upload = SimpleUploadedFile("receipt.exe", b"MZ-invalid", content_type="application/octet-stream")
        expense = self.client.post("/api/treasury/petty-cash-expenses/", {
            "fund": fund_id, "expense_date": "2026-03-02", "description": "مرفق غير مسموح",
            "amount": "20", "expense_account": self.expense_gl.pk, "receipt_upload": upload,
        }, format="multipart")
        self.assertEqual(expense.status_code, 400, expense.data)
        self.assertFalse(PettyCashExpense.objects.exists())


    def test_rejected_expense_can_be_corrected_then_resubmitted_and_approved(self):
        fund_id = self._create_fund()
        expense = self.client.post("/api/treasury/petty-cash-expenses/", {
            "fund": fund_id, "expense_date": "2026-03-02",
            "description": "مصروف يحتاج تصحيح", "amount": "15",
            "expense_account": self.expense_gl.pk, "receipt_number": "R-EDIT-1",
        }, format="json")
        self.assertEqual(expense.status_code, 201, expense.data)
        submitted = self.client.post(
            f"/api/treasury/petty-cash-expenses/{expense.data['id']}/submit/", {}, format="json",
        )
        self.assertEqual(submitted.status_code, 200, submitted.data)
        self.client.force_authenticate(self.approver)
        rejected = self.client.post(
            f"/api/treasury/petty-cash-expenses/{expense.data['id']}/reject/",
            {"rejection_note": "الإيصال يحتاج تصحيح البيان والمبلغ"},
            format="json",
        )
        self.assertEqual(rejected.status_code, 200, rejected.data)
        self.client.force_authenticate(self.user)
        corrected = self.client.patch(
            f"/api/treasury/petty-cash-expenses/{expense.data['id']}/",
            {"description": "شراء أدوات مكتبية بعد التصحيح", "amount": "18.5000",
             "receipt_number": "R-EDIT-2"},
            format="json",
        )
        self.assertEqual(corrected.status_code, 200, corrected.data)
        self.assertEqual(corrected.data["status"], PettyCashExpense.Status.REJECTED)
        self.assertEqual(Decimal(str(corrected.data["amount"])), Decimal("18.5000"))
        resubmitted = self.client.post(
            f"/api/treasury/petty-cash-expenses/{expense.data['id']}/submit/", {}, format="json",
        )
        self.assertEqual(resubmitted.status_code, 200, resubmitted.data)
        self.assertEqual(resubmitted.data["status"], PettyCashExpense.Status.PENDING_APPROVAL)
        self.assertEqual(resubmitted.data["rejection_note"], "")
        self.client.force_authenticate(self.approver)
        approved = self.client.post(
            f"/api/treasury/petty-cash-expenses/{expense.data['id']}/approve/",
            {"approval_note": "تمت مراجعة المستند بعد التصحيح"},
            format="json",
        )
        self.assertEqual(approved.status_code, 200, approved.data)
        self.assertEqual(approved.data["status"], PettyCashExpense.Status.APPROVED)

    def test_handover_rejects_backdated_date_before_latest_fund_movement(self):
        fund_id = self._create_fund()
        funded = self.client.post(
            f"/api/treasury/petty-cash-funds/{fund_id}/fund/",
            {"amount": "500", "transaction_date": "2026-03-01"}, format="json",
        )
        self.assertEqual(funded.status_code, 201, funded.data)
        expense = self.client.post("/api/treasury/petty-cash-expenses/", {
            "fund": fund_id, "expense_date": "2026-03-02",
            "description": "مصروف قبل التسليم", "amount": "100",
            "expense_account": self.expense_gl.pk, "receipt_number": "R-HANDOVER-1",
        }, format="json")
        self.assertEqual(expense.status_code, 201, expense.data)
        self.client.post(f"/api/treasury/petty-cash-expenses/{expense.data['id']}/submit/", {}, format="json")
        self.client.force_authenticate(self.approver)
        self.client.post(f"/api/treasury/petty-cash-expenses/{expense.data['id']}/approve/", {}, format="json")
        self.client.force_authenticate(self.user)
        settled = self.client.post(
            f"/api/treasury/petty-cash-funds/{fund_id}/settle/",
            {"expense_ids": [expense.data["id"]], "settlement_date": "2026-03-03"},
            format="json",
        )
        self.assertEqual(settled.status_code, 201, settled.data)
        backdated = self.client.post(
            f"/api/treasury/petty-cash-funds/{fund_id}/handover/",
            {"new_custodian": self.new_custodian.pk, "handover_date": "2026-03-02",
             "actual_balance": "400"},
            format="json",
        )
        self.assertEqual(backdated.status_code, 400, backdated.data)
        valid = self.client.post(
            f"/api/treasury/petty-cash-funds/{fund_id}/handover/",
            {"new_custodian": self.new_custodian.pk, "handover_date": "2026-03-04",
             "actual_balance": "400", "notes": "تسليم بعد آخر حركة مسجلة"},
            format="json",
        )
        self.assertEqual(valid.status_code, 201, valid.data)
        self.assertEqual(Decimal(str(valid.data["expected_balance"])), Decimal("400"))

    def test_suspended_fund_blocks_resubmission_and_approval(self):
        fund_id = self._create_fund()
        rejected_expense = self.client.post("/api/treasury/petty-cash-expenses/", {
            "fund": fund_id, "expense_date": "2026-03-02",
            "description": "مصروف مرفوض", "amount": "10",
            "expense_account": self.expense_gl.pk, "receipt_number": "R-SUSP-1",
        }, format="json")
        pending_expense = self.client.post("/api/treasury/petty-cash-expenses/", {
            "fund": fund_id, "expense_date": "2026-03-02",
            "description": "مصروف معلق", "amount": "20",
            "expense_account": self.expense_gl.pk, "receipt_number": "R-SUSP-2",
        }, format="json")
        for item in (rejected_expense, pending_expense):
            self.assertEqual(item.status_code, 201, item.data)
            submitted = self.client.post(
                f"/api/treasury/petty-cash-expenses/{item.data['id']}/submit/", {}, format="json",
            )
            self.assertEqual(submitted.status_code, 200, submitted.data)
        self.client.force_authenticate(self.approver)
        rejected = self.client.post(
            f"/api/treasury/petty-cash-expenses/{rejected_expense.data['id']}/reject/",
            {"rejection_note": "المستند غير مكتمل ويحتاج استيفاء"},
            format="json",
        )
        self.assertEqual(rejected.status_code, 200, rejected.data)
        self.client.force_authenticate(self.user)
        PettyCashFund.objects.filter(pk=fund_id).update(status=PettyCashFund.Status.SUSPENDED)
        resubmit = self.client.post(
            f"/api/treasury/petty-cash-expenses/{rejected_expense.data['id']}/submit/", {}, format="json",
        )
        self.assertEqual(resubmit.status_code, 400, resubmit.data)
        self.client.force_authenticate(self.approver)
        approve = self.client.post(
            f"/api/treasury/petty-cash-expenses/{pending_expense.data['id']}/approve/", {}, format="json",
        )
        self.assertEqual(approve.status_code, 400, approve.data)
        self.assertEqual(
            PettyCashExpense.objects.get(pk=pending_expense.data["id"]).status,
            PettyCashExpense.Status.PENDING_APPROVAL,
        )

    def test_petty_cash_statement_reconciles_funding_expense_and_count_difference(self):
        fund_id = self._create_fund()
        funded = self.client.post(
            f"/api/treasury/petty-cash-funds/{fund_id}/fund/",
            {"amount": "500", "transaction_date": "2026-03-01"}, format="json",
        )
        self.assertEqual(funded.status_code, 201, funded.data)
        expense = self.client.post("/api/treasury/petty-cash-expenses/", {
            "fund": fund_id, "expense_date": "2026-03-02",
            "description": "مصروف كشف الحركة", "amount": "100",
            "expense_account": self.expense_gl.pk, "receipt_number": "R-STMT-1",
        }, format="json")
        self.assertEqual(expense.status_code, 201, expense.data)
        self.client.post(f"/api/treasury/petty-cash-expenses/{expense.data['id']}/submit/", {}, format="json")
        self.client.force_authenticate(self.approver)
        self.client.post(f"/api/treasury/petty-cash-expenses/{expense.data['id']}/approve/", {}, format="json")
        self.client.force_authenticate(self.user)
        settled = self.client.post(
            f"/api/treasury/petty-cash-funds/{fund_id}/settle/",
            {"expense_ids": [expense.data["id"]], "settlement_date": "2026-03-03"},
            format="json",
        )
        self.assertEqual(settled.status_code, 201, settled.data)
        count = self.client.post(
            f"/api/treasury/petty-cash-funds/{fund_id}/count/",
            {"count_date": "2026-03-04", "actual_balance": "398", "notes": "عجز جرد بمقدار 2"},
            format="json",
        )
        self.assertEqual(count.status_code, 201, count.data)
        self.client.force_authenticate(self.approver)
        approved_count = self.client.post(
            f"/api/treasury/petty-cash-funds/{fund_id}/approve-count/",
            {"count_id": count.data["id"], "counter_account": self.expense_gl.pk,
             "review_note": "تم فحص العجز واعتماد المعالجة"},
            format="json",
        )
        self.assertEqual(approved_count.status_code, 200, approved_count.data)
        self.client.force_authenticate(self.user)
        statement = self.client.get(
            f"/api/treasury/petty-cash-funds/{fund_id}/statement/"
            "?date_from=2026-03-01&date_to=2026-03-31"
        )
        self.assertEqual(statement.status_code, 200, statement.data)
        self.assertEqual(Decimal(str(statement.data["opening_balance"])), Decimal("0"))
        self.assertEqual(Decimal(str(statement.data["total_inflows"])), Decimal("500"))
        self.assertEqual(Decimal(str(statement.data["total_outflows"])), Decimal("102"))
        self.assertEqual(Decimal(str(statement.data["closing_balance"])), Decimal("398"))
        self.assertEqual(Decimal(str(statement.data["reconciliation_difference"])), Decimal("0"))
        self.assertEqual(len(statement.data["rows"]), 3)

    def test_petty_cash_expense_report_groups_by_branch_account_and_lists_unsettled(self):
        fund_id = self._create_fund()
        rejected = self.client.post("/api/treasury/petty-cash-expenses/", {
            "fund": fund_id, "expense_date": "2026-03-02",
            "description": "مصروف مرفوض بالتقرير", "amount": "10",
            "expense_account": self.expense_gl.pk, "receipt_number": "R-REPORT-1",
        }, format="json")
        draft = self.client.post("/api/treasury/petty-cash-expenses/", {
            "fund": fund_id, "expense_date": "2026-03-04",
            "description": "مصروف مسودة بالتقرير", "amount": "20",
            "expense_account": self.expense_gl.pk, "receipt_number": "R-REPORT-2",
        }, format="json")
        self.assertEqual(rejected.status_code, 201, rejected.data)
        self.assertEqual(draft.status_code, 201, draft.data)
        self.client.post(f"/api/treasury/petty-cash-expenses/{rejected.data['id']}/submit/", {}, format="json")
        self.client.force_authenticate(self.approver)
        self.client.post(
            f"/api/treasury/petty-cash-expenses/{rejected.data['id']}/reject/",
            {"rejection_note": "المستند غير مكتمل ويحتاج استيفاء"},
            format="json",
        )
        self.client.force_authenticate(self.user)
        report = self.client.get(
            f"/api/treasury/petty-cash-expenses/report/?company={self.company.pk}"
            f"&branch={self.branch.pk}&date_from=2026-03-01&date_to=2026-03-31"
        )
        self.assertEqual(report.status_code, 200, report.data)
        self.assertEqual(Decimal(str(report.data["total_expenses"])), Decimal("30"))
        self.assertEqual(report.data["unsettled_count"], 2)
        self.assertEqual(Decimal(str(report.data["unsettled_total"])), Decimal("30"))
        self.assertEqual(len(report.data["by_branch_account"]), 1)
        row = report.data["by_branch_account"][0]
        self.assertEqual(row["fund__branch_id"], self.branch.pk)
        self.assertEqual(row["expense_account_id"], self.expense_gl.pk)
        self.assertEqual(Decimal(str(row["total_amount"])), Decimal("30"))
