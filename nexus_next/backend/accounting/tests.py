from datetime import date
from decimal import Decimal
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.contrib.auth.models import Permission
from rest_framework.test import APIClient

from accounts.models import User
from companies.models import Branch, Company, CompanyActivity, CompanyMembership, Currency, ExchangeRate, FiscalPeriod, FiscalYear
from audit.models import AuditEvent
from treasury.models import (TreasuryAccount, TreasuryTransaction, TreasuryFxRevaluation,
                             PettyCashFund, PettyCashTransfer, PettyCashSettlement,
                             PettyCashExpense, PettyCashCount)
from treasury.services import (
    post_treasury_transaction, revalue_treasury_fx_account,
    reverse_treasury_fx_revaluation, reverse_treasury_transaction,
)
from .models import AccountRole, ChartOfAccount, CostCenter, JournalEntry, JournalLine, TaxCode
from .services import post_journal, reverse_operational_journal


class PostingEngineTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        currency = Currency.objects.create(code="EGP", name_ar="جنيه مصري")
        cls.company = Company.objects.create(code="TST", name_ar="شركة اختبار", country="EG",
            activity="commercial", base_currency=currency)
        cls.branch = Branch.objects.create(company=cls.company, code="HQ", name_ar="الرئيسي", is_head_office=True)
        cls.year = FiscalYear.objects.create(company=cls.company, name="2026",
            start_date=date(2026, 1, 1), end_date=date(2026, 12, 31))
        cls.cash = ChartOfAccount.objects.create(company=cls.company, code="1001",
            name_ar="الصندوق", nature="asset")
        cls.capital = ChartOfAccount.objects.create(company=cls.company, code="3001",
            name_ar="رأس المال", nature="equity")

    def make_entry(self, debit="100.00", credit="100.00"):
        entry = JournalEntry.objects.create(company=self.company, branch=self.branch,
            fiscal_year=self.year, entry_number="JV-001", entry_date=date(2026, 1, 2),
            description="اختبار")
        JournalLine.objects.create(entry=entry, account=self.cash, debit=Decimal(debit))
        JournalLine.objects.create(entry=entry, account=self.capital, credit=Decimal(credit))
        return entry

    def test_balanced_entry_posts(self):
        entry = self.make_entry()
        post_journal(entry.pk)
        entry.refresh_from_db()
        self.assertEqual(entry.status, JournalEntry.Status.POSTED)
        event = AuditEvent.objects.get(
            resource="accounting.journal_entry.posted", object_id=str(entry.pk)
        )
        self.assertEqual(event.company, self.company)
        self.assertEqual(event.branch, self.branch)
        self.assertTrue(event.succeeded)
        self.assertEqual(event.metadata["entry_number"], entry.entry_number)

    def test_posting_rolls_back_if_transactional_audit_write_fails(self):
        from unittest.mock import patch

        entry = self.make_entry()
        with patch("accounting.services.AuditEvent.objects.create", side_effect=RuntimeError("audit unavailable")):
            with self.assertRaises(RuntimeError):
                post_journal(entry.pk)
        entry.refresh_from_db()
        self.assertEqual(entry.status, JournalEntry.Status.DRAFT)
        self.assertFalse(AuditEvent.objects.filter(
            resource="accounting.journal_entry.posted", object_id=str(entry.pk)
        ).exists())

    def test_reversal_audits_original_and_reversal_in_same_transaction(self):
        original = self.make_entry()
        post_journal(original.pk)
        reversal = JournalEntry.objects.create(
            company=self.company, branch=self.branch, fiscal_year=self.year,
            entry_number="JV-REV-002", entry_date=date(2026, 1, 3),
            description="عكس قيد", reversal_of=original,
        )
        JournalLine.objects.create(entry=reversal, account=self.cash, credit=Decimal("100"))
        JournalLine.objects.create(entry=reversal, account=self.capital, debit=Decimal("100"))
        post_journal(reversal.pk)

        original.refresh_from_db()
        reversal.refresh_from_db()
        self.assertEqual(original.status, JournalEntry.Status.REVERSED)
        self.assertEqual(reversal.status, JournalEntry.Status.POSTED)
        self.assertTrue(AuditEvent.objects.filter(
            resource="accounting.journal_entry.reversed",
            object_id=str(original.pk),
            metadata__reversal_entry_id=reversal.pk,
        ).exists())
        self.assertTrue(AuditEvent.objects.filter(
            resource="accounting.journal_entry.posted",
            object_id=str(reversal.pk),
            metadata__reversal_of_id=original.pk,
        ).exists())

    def test_journal_reversal_api_posts_exact_mirror_and_prevents_second_reversal(self):
        user = User.objects.create_user(username="journal-reverser", password="test-password")
        CompanyMembership.objects.create(user=user, company=self.company, role="accountant", all_branches=True)
        client = APIClient()
        client.force_authenticate(user)
        original = self.make_entry()
        post_journal(original.pk)

        response = client.post(
            f"/api/accounting/journal-entries/{original.pk}/reverse/",
            {"entry_date": "2026-01-03"}, format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        original.refresh_from_db()
        reversal = JournalEntry.objects.get(pk=response.data["id"])
        self.assertEqual(original.status, JournalEntry.Status.REVERSED)
        self.assertEqual(reversal.status, JournalEntry.Status.POSTED)
        self.assertEqual(reversal.reversal_of_id, original.pk)
        self.assertEqual(
            list(reversal.lines.order_by("account_id").values_list("account_id", "debit", "credit")),
            list(original.lines.order_by("account_id").values_list("account_id", "credit", "debit")),
        )

        second = client.post(
            f"/api/accounting/journal-entries/{original.pk}/reverse/", {}, format="json"
        )
        reverse_of_reverse = client.post(
            f"/api/accounting/journal-entries/{reversal.pk}/reverse/", {}, format="json"
        )
        self.assertEqual(second.status_code, 400, second.data)
        self.assertEqual(reverse_of_reverse.status_code, 400, reverse_of_reverse.data)
        self.assertEqual(JournalEntry.objects.filter(reversal_of=original).count(), 1)

    def test_general_reversal_api_rejects_linked_treasury_document(self):
        user = User.objects.create_user(username="treasury-reversal-guard", password="test-password")
        CompanyMembership.objects.create(user=user, company=self.company, role="accountant", all_branches=True)
        client = APIClient()
        client.force_authenticate(user)
        original = self.make_entry()
        post_journal(original.pk)
        treasury_account = TreasuryAccount.objects.create(
            company=self.company, branch=self.branch, account=self.cash,
            kind=TreasuryAccount.Kind.CASH, code="CASH-GUARD", name_ar="خزينة الاختبار",
        )
        TreasuryTransaction.objects.create(
            company=self.company, branch=self.branch, fiscal_year=self.year,
            source_account=treasury_account, counter_account=self.capital,
            kind=TreasuryTransaction.Kind.RECEIPT,
            status=TreasuryTransaction.Status.POSTED,
            document_number="TR-GUARD-001", transaction_date=date(2026, 1, 2),
            amount=Decimal("100.0000"), description="حركة خزينة مرتبطة بالقيد",
            journal_entry=original,
        )

        response = client.post(
            f"/api/accounting/journal-entries/{original.pk}/reverse/",
            {"entry_date": "2026-01-03"}, format="json",
        )

        self.assertEqual(response.status_code, 400, response.data)
        original.refresh_from_db()
        self.assertEqual(original.status, JournalEntry.Status.POSTED)
        self.assertFalse(JournalEntry.objects.filter(reversal_of=original).exists())

    def test_unbalanced_entry_is_rejected(self):
        entry = self.make_entry(credit="90.00")
        with self.assertRaises(ValidationError):
            post_journal(entry.pk)

    def test_opening_journal_rejects_income_statement_accounts(self):
        revenue = ChartOfAccount.objects.create(
            company=self.company, code="4001", name_ar="إيرادات الاختبار",
            nature=ChartOfAccount.Nature.REVENUE,
        )
        entry = JournalEntry.objects.create(
            company=self.company, branch=self.branch, fiscal_year=self.year,
            entry_number="OPEN-PL-001", entry_date=self.year.start_date,
            description="اختبار منع حسابات الدخل في الافتتاحي",
            source_type=JournalEntry.Source.OPENING,
        )
        JournalLine.objects.create(entry=entry, account=self.cash, debit=Decimal("100"))
        JournalLine.objects.create(entry=entry, account=revenue, credit=Decimal("100"))

        with self.assertRaises(ValidationError):
            post_journal(entry.pk)
        entry.refresh_from_db()
        self.assertEqual(entry.status, JournalEntry.Status.DRAFT)

    def test_opening_journal_must_use_fiscal_year_start_date(self):
        entry = JournalEntry.objects.create(
            company=self.company, branch=self.branch, fiscal_year=self.year,
            entry_number="OPEN-DATE-001", entry_date=date(2026, 1, 2),
            description="اختبار تاريخ القيد الافتتاحي",
            source_type=JournalEntry.Source.OPENING,
        )
        JournalLine.objects.create(entry=entry, account=self.cash, debit=Decimal("100"))
        JournalLine.objects.create(entry=entry, account=self.capital, credit=Decimal("100"))

        with self.assertRaises(ValidationError):
            post_journal(entry.pk)
        entry.refresh_from_db()
        self.assertEqual(entry.status, JournalEntry.Status.DRAFT)

    def test_closing_journal_must_use_fiscal_year_end_date(self):
        entry = JournalEntry.objects.create(
            company=self.company, branch=self.branch, fiscal_year=self.year,
            entry_number="CLOSE-DATE-001", entry_date=date(2026, 12, 30),
            description="اختبار تاريخ قيد الإقفال",
            source_type=JournalEntry.Source.CLOSING,
        )
        JournalLine.objects.create(entry=entry, account=self.cash, debit=Decimal("100"))
        JournalLine.objects.create(entry=entry, account=self.capital, credit=Decimal("100"))

        with self.assertRaises(ValidationError):
            post_journal(entry.pk)
        entry.refresh_from_db()
        self.assertEqual(entry.status, JournalEntry.Status.DRAFT)

    def test_closed_monthly_period_rejects_journal_posting(self):
        FiscalPeriod.objects.create(
            fiscal_year=self.year, name="2026-01",
            start_date=date(2026, 1, 1), end_date=date(2026, 1, 31),
            status=FiscalPeriod.Status.CLOSED,
        )
        entry = self.make_entry()
        with self.assertRaises(ValidationError):
            post_journal(entry.pk)
        entry.refresh_from_db()
        self.assertEqual(entry.status, JournalEntry.Status.DRAFT)

    def test_closed_year_rejects_posting(self):
        self.year.status = FiscalYear.Status.CLOSED
        self.year.save(update_fields=["status"])
        entry = self.make_entry()
        with self.assertRaises(ValidationError):
            post_journal(entry.pk)

    def test_posted_entry_and_lines_are_immutable(self):
        entry = self.make_entry()
        post_journal(entry.pk)
        entry.refresh_from_db()
        with self.assertRaises(ValidationError):
            entry.description = "تعديل غير مسموح"
            entry.save()
        line = entry.lines.first()
        with self.assertRaises(ValidationError):
            line.description = "تعديل غير مسموح"
            line.save()

    def test_reversal_must_exactly_mirror_original(self):
        original = self.make_entry()
        post_journal(original.pk)
        reversal = JournalEntry.objects.create(company=self.company, branch=self.branch,
            fiscal_year=self.year, entry_number="JV-REV-001", entry_date=date(2026, 1, 3),
            description="عكس قيد", reversal_of=original)
        JournalLine.objects.create(entry=reversal, account=self.cash, credit=Decimal("99"))
        JournalLine.objects.create(entry=reversal, account=self.capital, debit=Decimal("99"))
        with self.assertRaises(ValidationError):
            post_journal(reversal.pk)

    def test_account_from_another_company_is_rejected(self):
        other_currency = Currency.objects.create(code="SAR", name_ar="ريال سعودي")
        other_company = Company.objects.create(code="OTH", name_ar="شركة أخرى", country="SA",
            activity="service", base_currency=other_currency)
        other_account = ChartOfAccount.objects.create(company=other_company, code="1001",
            name_ar="صندوق آخر", nature="asset")
        entry = JournalEntry.objects.create(company=self.company, branch=self.branch,
            fiscal_year=self.year, entry_number="JV-002", entry_date=date(2026, 1, 2), description="اختبار")
        JournalLine.objects.create(entry=entry, account=other_account, debit=Decimal("10"))
        JournalLine.objects.create(entry=entry, account=self.capital, credit=Decimal("10"))
        with self.assertRaises(ValidationError):
            post_journal(entry.pk)




class DirectCashFlowApiTests(TestCase):
    def test_direct_cash_flow_groups_receipts_and_payments_and_reconciles_to_cash_ledger(self):
        currency = Currency.objects.create(code="EGP", name_ar="جنيه مصري")
        company = Company.objects.create(
            code="DIRECT-CF", name_ar="شركة التدفقات المباشرة",
            country="EG", activity="commercial", base_currency=currency,
        )
        branch = Branch.objects.create(
            company=company, code="DIRECT-HQ", name_ar="الرئيسي",
            is_head_office=True,
        )
        year = FiscalYear.objects.create(
            company=company, name="2026",
            start_date=date(2026, 1, 1), end_date=date(2026, 12, 31),
        )
        cash = ChartOfAccount.objects.create(
            company=company, code="1001", name_ar="البنك", nature="asset",
        )
        revenue = ChartOfAccount.objects.create(
            company=company, code="4001", name_ar="إيرادات العملاء", nature="revenue",
        )
        expense = ChartOfAccount.objects.create(
            company=company, code="5001", name_ar="مصروفات تشغيلية", nature="expense",
        )
        fixed_asset = ChartOfAccount.objects.create(
            company=company, code="1501", name_ar="أصول ثابتة", nature="asset",
        )
        loan = ChartOfAccount.objects.create(
            company=company, code="2501", name_ar="قرض بنكي", nature="liability",
        )
        capital = ChartOfAccount.objects.create(
            company=company, code="3001", name_ar="رأس المال", nature="equity",
        )
        opening_entry = JournalEntry.objects.create(
            company=company, branch=branch, fiscal_year=year,
            entry_number="DIRECT-CF-OPENING", entry_date=date(2026, 1, 1),
            description="رصيد نقدية افتتاحي", source_type=JournalEntry.Source.OPENING,
        )
        JournalLine.objects.create(entry=opening_entry, account=cash, debit=Decimal("500.0000"))
        JournalLine.objects.create(entry=opening_entry, account=capital, credit=Decimal("500.0000"))
        post_journal(opening_entry.pk)

        treasury = TreasuryAccount.objects.create(
            company=company, branch=branch, account=cash,
            kind=TreasuryAccount.Kind.BANK, code="BANK-01",
            name_ar="البنك الرئيسي", currency_code="EGP",
        )

        movements = (
            ("DIRECT-RECEIPT", TreasuryTransaction.Kind.RECEIPT, revenue, "150.0000",
             TreasuryTransaction.CashFlowCategory.OPERATING),
            ("DIRECT-OPERATING-PAYMENT", TreasuryTransaction.Kind.PAYMENT, expense, "50.0000",
             TreasuryTransaction.CashFlowCategory.OPERATING),
            ("DIRECT-INVESTING-PAYMENT", TreasuryTransaction.Kind.PAYMENT, fixed_asset, "30.0000",
             TreasuryTransaction.CashFlowCategory.INVESTING),
            ("DIRECT-FINANCING-RECEIPT", TreasuryTransaction.Kind.RECEIPT, loan, "100.0000",
             TreasuryTransaction.CashFlowCategory.FINANCING),
        )
        for number, kind, counter_account, amount, category in movements:
            entry = JournalEntry.objects.create(
                company=company, branch=branch, fiscal_year=year,
                entry_number=number, entry_date=date(2026, 1, 15),
                description=number,
            )
            if kind == TreasuryTransaction.Kind.RECEIPT:
                JournalLine.objects.create(entry=entry, account=cash, debit=Decimal(amount))
                JournalLine.objects.create(entry=entry, account=counter_account, credit=Decimal(amount))
            else:
                JournalLine.objects.create(entry=entry, account=counter_account, debit=Decimal(amount))
                JournalLine.objects.create(entry=entry, account=cash, credit=Decimal(amount))
            post_journal(entry.pk)
            TreasuryTransaction.objects.create(
                company=company, branch=branch, fiscal_year=year,
                source_account=treasury, counter_account=counter_account,
                kind=kind, status=TreasuryTransaction.Status.POSTED,
                document_number=number, transaction_date=date(2026, 1, 15),
                amount=Decimal(amount), description=number,
                cash_flow_category=category, journal_entry=entry,
            )

        user = User.objects.create_user(username="direct_cashflow_reader", password="test-password")
        user.user_permissions.add(Permission.objects.get(codename="view_journalentry"))
        CompanyMembership.objects.create(
            user=user, company=company, role="viewer", all_branches=True,
        )
        client = APIClient()
        client.force_authenticate(user=user)
        result = client.get(
            f"/api/accounting/journal-entries/direct-cash-flow/?company={company.pk}"
            "&date_from=2026-01-01&date_to=2026-01-31"
        )

        self.assertEqual(result.status_code, 200, result.data)
        self.assertEqual(result.data["method"], "direct")
        sections = {row["category"]: row for row in result.data["sections"]}
        self.assertEqual(sections[TreasuryTransaction.CashFlowCategory.OPERATING]["cash_receipts"], Decimal("150.0000"))
        self.assertEqual(sections[TreasuryTransaction.CashFlowCategory.OPERATING]["cash_payments"], Decimal("50.0000"))
        self.assertEqual(sections[TreasuryTransaction.CashFlowCategory.OPERATING]["net_cash_flow"], Decimal("100.0000"))
        self.assertEqual(sections[TreasuryTransaction.CashFlowCategory.INVESTING]["net_cash_flow"], Decimal("-30.0000"))
        self.assertEqual(sections[TreasuryTransaction.CashFlowCategory.FINANCING]["net_cash_flow"], Decimal("100.0000"))
        self.assertEqual(result.data["opening_cash_balance"], Decimal("500.0000"))
        self.assertEqual(result.data["net_change_in_cash"], Decimal("170.0000"))
        self.assertEqual(result.data["closing_cash_balance"], Decimal("670.0000"))
        self.assertEqual(result.data["ledger_cash_change"], Decimal("170.0000"))
        self.assertEqual(result.data["ledger_closing_cash_balance"], Decimal("670.0000"))
        self.assertEqual(result.data["closing_balance_difference"], Decimal("0.0000"))
        self.assertEqual(result.data["reconciliation_difference"], Decimal("0.0000"))
        self.assertEqual(result.data["non_cash_investing_transactions"], [])
        self.assertEqual(result.data["uncategorized_counterparty_transactions"], [])

    def test_direct_cash_flow_keeps_treasury_reversal_in_its_own_reporting_period(self):
        currency = Currency.objects.create(code="EGP", name_ar="جنيه مصري")
        company = Company.objects.create(
            code="DIRECT-REV-CF", name_ar="شركة اختبار عكس الخزينة",
            country="EG", activity="commercial", base_currency=currency,
        )
        branch = Branch.objects.create(
            company=company, code="REV-HQ", name_ar="الرئيسي", is_head_office=True,
        )
        year = FiscalYear.objects.create(
            company=company, name="2026",
            start_date=date(2026, 1, 1), end_date=date(2026, 12, 31),
        )
        cash = ChartOfAccount.objects.create(
            company=company, code="1001", name_ar="البنك", nature="asset",
        )
        revenue = ChartOfAccount.objects.create(
            company=company, code="4001", name_ar="الإيرادات", nature="revenue",
        )
        capital = ChartOfAccount.objects.create(
            company=company, code="3001", name_ar="رأس المال", nature="equity",
        )
        opening = JournalEntry.objects.create(
            company=company, branch=branch, fiscal_year=year,
            entry_number="REV-CF-OPEN", entry_date=date(2026, 1, 1),
            description="رصيد افتتاحي", source_type=JournalEntry.Source.OPENING,
        )
        JournalLine.objects.create(entry=opening, account=cash, debit=Decimal("500"))
        JournalLine.objects.create(entry=opening, account=capital, credit=Decimal("500"))
        post_journal(opening.pk)
        bank = TreasuryAccount.objects.create(
            company=company, branch=branch, account=cash,
            kind=TreasuryAccount.Kind.BANK, code="REV-CF-BANK",
            name_ar="البنك", currency_code="EGP",
        )
        user = User.objects.create_user(username="direct_reversal_reader", password="test-password")
        user.user_permissions.add(Permission.objects.get(codename="view_journalentry"))
        CompanyMembership.objects.create(user=user, company=company, role="viewer", all_branches=True)

        receipt = TreasuryTransaction.objects.create(
            company=company, branch=branch, fiscal_year=year,
            source_account=bank, counter_account=revenue,
            kind=TreasuryTransaction.Kind.RECEIPT,
            document_number="REV-CF-RECEIPT", transaction_date=date(2026, 1, 15),
            amount=Decimal("100"), description="تحصيل عميل",
            cash_flow_category=TreasuryTransaction.CashFlowCategory.OPERATING,
        )
        post_treasury_transaction(receipt.pk, user)
        reverse_treasury_transaction(
            transaction_id=receipt.pk, entry_date=date(2026, 2, 15), user=user,
        )

        client = APIClient()
        client.force_authenticate(user=user)
        january = client.get(
            f"/api/accounting/journal-entries/direct-cash-flow/?company={company.pk}"
            "&date_from=2026-01-01&date_to=2026-01-31"
        )
        self.assertEqual(january.status_code, 200, january.data)
        jan_operating = next(
            row for row in january.data["sections"]
            if row["category"] == TreasuryTransaction.CashFlowCategory.OPERATING
        )
        self.assertEqual(jan_operating["cash_receipts"], Decimal("100.0000"))
        self.assertEqual(january.data["ledger_cash_change"], Decimal("100.0000"))
        self.assertEqual(january.data["reconciliation_difference"], Decimal("0.0000"))

        february = client.get(
            f"/api/accounting/journal-entries/direct-cash-flow/?company={company.pk}"
            "&date_from=2026-02-01&date_to=2026-02-28"
        )
        self.assertEqual(february.status_code, 200, february.data)
        feb_operating = next(
            row for row in february.data["sections"]
            if row["category"] == TreasuryTransaction.CashFlowCategory.OPERATING
        )
        self.assertEqual(feb_operating["cash_payments"], Decimal("100.0000"))
        self.assertEqual(february.data["opening_cash_balance"], Decimal("600.0000"))
        self.assertEqual(february.data["net_change_in_cash"], Decimal("-100.0000"))
        self.assertEqual(february.data["ledger_cash_change"], Decimal("-100.0000"))
        self.assertEqual(february.data["closing_cash_balance"], Decimal("500.0000"))
        self.assertEqual(february.data["reconciliation_difference"], Decimal("0.0000"))

    def test_direct_cash_flow_reports_fx_effect_separately_and_scopes_it_to_branch(self):
        currency = Currency.objects.create(code="EGP", name_ar="جنيه مصري")
        usd = Currency.objects.create(code="USD", name_ar="دولار أمريكي")
        company = Company.objects.create(
            code="DIRECT-FX-CF", name_ar="شركة اختبار أثر العملة",
            country="EG", activity="commercial", base_currency=currency,
        )
        branch_one = Branch.objects.create(
            company=company, code="FX-HQ", name_ar="الرئيسي", is_head_office=True,
        )
        branch_two = Branch.objects.create(
            company=company, code="FX-SEC", name_ar="الفرع الثاني",
        )
        year = FiscalYear.objects.create(
            company=company, name="2026",
            start_date=date(2026, 1, 1), end_date=date(2026, 12, 31),
        )
        counter = ChartOfAccount.objects.create(
            company=company, code="4001", name_ar="إيرادات", nature="revenue",
        )
        fx_gain = ChartOfAccount.objects.create(
            company=company, code="4901", name_ar="أرباح فروق عملة غير محققة",
            nature="revenue",
        )
        AccountRole.objects.create(
            company=company, role=AccountRole.Role.UNREALIZED_FX_GAIN, account=fx_gain,
        )
        ExchangeRate.objects.create(
            company=company, currency=usd, rate_date=date(2026, 1, 15),
            rate=Decimal("30"),
        )
        ExchangeRate.objects.create(
            company=company, currency=usd, rate_date=date(2026, 1, 20),
            rate=Decimal("32"),
        )

        for branch, suffix, ledger_code in (
            (branch_one, "ONE", "1120"),
            (branch_two, "TWO", "1121"),
        ):
            ledger = ChartOfAccount.objects.create(
                company=company, code=ledger_code, name_ar=f"بنك دولار {suffix}",
                nature="asset",
            )
            treasury = TreasuryAccount.objects.create(
                company=company, branch=branch, account=ledger,
                kind=TreasuryAccount.Kind.BANK, code=f"USD-{suffix}",
                name_ar=f"حساب دولار {suffix}", currency_code="USD",
            )
            receipt = TreasuryTransaction.objects.create(
                company=company, branch=branch, fiscal_year=year,
                source_account=treasury, counter_account=counter,
                kind=TreasuryTransaction.Kind.RECEIPT,
                document_number=f"FX-RECEIPT-{suffix}",
                transaction_date=date(2026, 1, 15), amount=Decimal("3000"),
                transaction_currency=usd, transaction_amount=Decimal("100"),
                exchange_rate=Decimal("30"), exchange_rate_date=date(2026, 1, 15),
                cash_flow_category=TreasuryTransaction.CashFlowCategory.OPERATING,
                description=f"تحصيل أجنبي {suffix}",
            )
            post_treasury_transaction(receipt.pk)
            revalue_treasury_fx_account(
                account_id=treasury.pk, fiscal_year_id=year.pk,
                revaluation_date=date(2026, 1, 20), rate_date=date(2026, 1, 20),
                branch=branch,
            )

        user = User.objects.create_user(username="direct_fx_reader", password="test-password")
        user.user_permissions.add(Permission.objects.get(codename="view_journalentry"))
        CompanyMembership.objects.create(
            user=user, company=company, role="viewer", all_branches=True,
        )
        client = APIClient()
        client.force_authenticate(user=user)
        url = (
            f"/api/accounting/journal-entries/direct-cash-flow/?company={company.pk}"
            "&date_from=2026-01-01&date_to=2026-01-31"
        )

        company_report = client.get(url)
        self.assertEqual(company_report.status_code, 200, company_report.data)
        self.assertEqual(company_report.data["net_change_in_cash"], Decimal("6000.0000"))
        self.assertEqual(company_report.data["effect_of_exchange_rate_changes"], Decimal("400.0000"))
        self.assertEqual(company_report.data["net_increase_decrease_in_cash"], Decimal("6400.0000"))
        self.assertEqual(company_report.data["closing_cash_balance"], Decimal("6400.0000"))
        self.assertEqual(company_report.data["ledger_closing_cash_balance"], Decimal("6400.0000"))
        self.assertEqual(company_report.data["ledger_cash_change"], Decimal("6400.0000"))
        self.assertEqual(company_report.data["reconciliation_difference"], Decimal("0.0000"))
        self.assertEqual(company_report.data["closing_balance_difference"], Decimal("0.0000"))
        self.assertEqual(len(company_report.data["exchange_rate_effect_details"]), 2)

        branch_report = client.get(f"{url}&branch={branch_one.pk}")
        self.assertEqual(branch_report.status_code, 200, branch_report.data)
        self.assertEqual(branch_report.data["effect_of_exchange_rate_changes"], Decimal("200.0000"))
        self.assertEqual(branch_report.data["net_increase_decrease_in_cash"], Decimal("3200.0000"))
        self.assertEqual(branch_report.data["ledger_closing_cash_balance"], Decimal("3200.0000"))
        self.assertEqual(branch_report.data["reconciliation_difference"], Decimal("0.0000"))
        
        first_revaluation = TreasuryFxRevaluation.objects.get(account__code="USD-ONE")
        reverse_treasury_fx_revaluation(
            revaluation_id=first_revaluation.pk,
            entry_date=date(2026, 2, 2),
        )

        # Historical January reporting must retain the original revaluation event
        # even though its journal is now marked REVERSED.
        january_after_reversal = client.get(url)
        self.assertEqual(january_after_reversal.status_code, 200, january_after_reversal.data)
        self.assertEqual(
            january_after_reversal.data["effect_of_exchange_rate_changes"],
            Decimal("400.0000"),
        )
        self.assertEqual(
            january_after_reversal.data["closing_cash_balance"],
            Decimal("6400.0000"),
        )

        february_url = (
            f"/api/accounting/journal-entries/direct-cash-flow/?company={company.pk}"
            "&date_from=2026-02-01&date_to=2026-02-28"
        )
        february_report = client.get(february_url)
        self.assertEqual(february_report.status_code, 200, february_report.data)
        self.assertEqual(february_report.data["opening_cash_balance"], Decimal("6400.0000"))
        self.assertEqual(february_report.data["net_change_in_cash"], Decimal("0.0000"))
        self.assertEqual(
            february_report.data["effect_of_exchange_rate_changes"],
            Decimal("-200.0000"),
        )
        self.assertEqual(
            february_report.data["net_increase_decrease_in_cash"],
            Decimal("-200.0000"),
        )
        self.assertEqual(february_report.data["closing_cash_balance"], Decimal("6200.0000"))
        self.assertEqual(
            february_report.data["ledger_closing_cash_balance"],
            Decimal("6200.0000"),
        )
        self.assertEqual(february_report.data["ledger_cash_change"], Decimal("-200.0000"))
        self.assertEqual(february_report.data["reconciliation_difference"], Decimal("0.0000"))
        self.assertEqual(
            [row["event_type"] for row in february_report.data["exchange_rate_effect_details"]],
            ["reversal"],
        )

        branch_february = client.get(f"{february_url}&branch={branch_one.pk}")
        self.assertEqual(branch_february.status_code, 200, branch_february.data)
        self.assertEqual(
            branch_february.data["effect_of_exchange_rate_changes"],
            Decimal("-200.0000"),
        )
        self.assertEqual(branch_february.data["closing_cash_balance"], Decimal("3000.0000"))
        self.assertEqual(
            branch_february.data["ledger_closing_cash_balance"],
            Decimal("3000.0000"),
        )
        self.assertEqual(branch_february.data["reconciliation_difference"], Decimal("0.0000"))

        january_indirect = client.get(
            f"/api/accounting/journal-entries/indirect-cash-flow/?company={company.pk}"
            "&date_from=2026-01-01&date_to=2026-01-31"
        )
        self.assertEqual(january_indirect.status_code, 200, january_indirect.data)
        self.assertEqual(january_indirect.data["operating_cash_flow"], Decimal("6000.0000"))
        self.assertEqual(
            january_indirect.data["effect_of_exchange_rate_changes"],
            Decimal("400.0000"),
        )
        self.assertEqual(
            january_indirect.data["net_increase_decrease_in_cash"],
            Decimal("6400.0000"),
        )
        self.assertEqual(january_indirect.data["closing_cash_balance"], Decimal("6400.0000"))
        self.assertEqual(
            january_indirect.data["ledger_closing_cash_balance"],
            Decimal("6400.0000"),
        )
        self.assertEqual(january_indirect.data["reconciliation_difference"], Decimal("0.0000"))

        february_indirect = client.get(
            f"/api/accounting/journal-entries/indirect-cash-flow/?company={company.pk}"
            "&date_from=2026-02-01&date_to=2026-02-28"
        )
        self.assertEqual(february_indirect.status_code, 200, february_indirect.data)
        self.assertEqual(february_indirect.data["operating_cash_flow"], Decimal("0.0000"))
        self.assertEqual(february_indirect.data["effect_of_exchange_rate_changes"], Decimal("-200.0000"))
        self.assertEqual(february_indirect.data["net_increase_decrease_in_cash"], Decimal("-200.0000"))
        self.assertEqual(february_indirect.data["opening_cash_balance"], Decimal("6400.0000"))
        self.assertEqual(february_indirect.data["closing_cash_balance"], Decimal("6200.0000"))
        self.assertEqual(
            february_indirect.data["ledger_closing_cash_balance"],
            Decimal("6200.0000"),
        )
        self.assertEqual(february_indirect.data["reconciliation_difference"], Decimal("0.0000"))


    def test_direct_cash_flow_includes_petty_cash_settlements_and_approved_count_variances(self):
        currency = Currency.objects.create(code="EGP", name_ar="جنيه مصري")
        company = Company.objects.create(
            code="DIRECT-PC-CF", name_ar="شركة تدفقات العهدة",
            country="EG", activity="commercial", base_currency=currency,
        )
        branch = Branch.objects.create(
            company=company, code="PC-HQ", name_ar="الرئيسي", is_head_office=True,
        )
        year = FiscalYear.objects.create(
            company=company, name="2026",
            start_date=date(2026, 1, 1), end_date=date(2026, 12, 31),
        )
        cash_gl = ChartOfAccount.objects.create(
            company=company, code="1001", name_ar="البنك الرئيسي", nature="asset",
        )
        petty_gl = ChartOfAccount.objects.create(
            company=company, code="1002", name_ar="العهدة النثرية", nature="asset",
        )
        capital = ChartOfAccount.objects.create(
            company=company, code="3001", name_ar="رأس المال", nature="equity",
        )
        expense = ChartOfAccount.objects.create(
            company=company, code="5001", name_ar="مصروفات العهدة", nature="expense",
        )
        opening = JournalEntry.objects.create(
            company=company, branch=branch, fiscal_year=year,
            entry_number="PC-CF-OPEN", entry_date=date(2026, 1, 1),
            description="رصيد نقدية افتتاحي", source_type=JournalEntry.Source.OPENING,
        )
        JournalLine.objects.create(entry=opening, account=cash_gl, debit=Decimal("500"))
        JournalLine.objects.create(entry=opening, account=capital, credit=Decimal("500"))
        post_journal(opening.pk)

        bank = TreasuryAccount.objects.create(
            company=company, branch=branch, account=cash_gl,
            kind=TreasuryAccount.Kind.BANK, code="PC-CF-BANK",
            name_ar="البنك", currency_code="EGP",
        )
        petty_account = TreasuryAccount.objects.create(
            company=company, branch=branch, account=petty_gl,
            kind=TreasuryAccount.Kind.CASH, code="PC-CF-CASH",
            name_ar="صندوق العهدة", currency_code="EGP",
        )
        user = User.objects.create_user(username="petty_cash_cf_reader", password="test-password")
        approver = User.objects.create_user(username="petty_cash_cf_approver", password="test-password")
        user.user_permissions.add(Permission.objects.get(codename="view_journalentry"))
        CompanyMembership.objects.create(user=user, company=company, role="viewer", all_branches=True)
        fund = PettyCashFund.objects.create(
            company=company, branch=branch, code="PC-CF-01", name="عهدة اختبار التدفقات",
            custodian=user, funding_account=bank, petty_cash_account=petty_account,
            authorized_amount=Decimal("500"), created_by=user,
        )

        transfer_entry = JournalEntry.objects.create(
            company=company, branch=branch, fiscal_year=year,
            entry_number="PC-CF-FUND", entry_date=date(2026, 1, 15),
            description="تمويل العهدة",
        )
        JournalLine.objects.create(entry=transfer_entry, account=petty_gl, debit=Decimal("200"))
        JournalLine.objects.create(entry=transfer_entry, account=cash_gl, credit=Decimal("200"))
        post_journal(transfer_entry.pk)
        transfer_doc = TreasuryTransaction.objects.create(
            company=company, branch=branch, fiscal_year=year,
            source_account=bank, destination_account=petty_account,
            kind=TreasuryTransaction.Kind.TRANSFER,
            status=TreasuryTransaction.Status.POSTED,
            document_number="PC-CF-TRANSFER", transaction_date=date(2026, 1, 15),
            amount=Decimal("200"), description="تمويل العهدة", journal_entry=transfer_entry,
        )
        PettyCashTransfer.objects.create(
            fund=fund, kind=PettyCashTransfer.Kind.INITIAL,
            amount=Decimal("200"), treasury_transaction=transfer_doc, created_by=user,
        )

        settlement_entry = JournalEntry.objects.create(
            company=company, branch=branch, fiscal_year=year,
            entry_number="PC-CF-SETTLE", entry_date=date(2026, 1, 16),
            description="تسوية مصروفات العهدة",
        )
        JournalLine.objects.create(entry=settlement_entry, account=expense, debit=Decimal("50"))
        JournalLine.objects.create(entry=settlement_entry, account=petty_gl, credit=Decimal("50"))
        post_journal(settlement_entry.pk)
        settlement = PettyCashSettlement.objects.create(
            fund=fund, settlement_date=date(2026, 1, 16),
            journal_entry=settlement_entry, total_amount=Decimal("50"), created_by=user,
        )
        PettyCashExpense.objects.create(
            fund=fund, expense_date=date(2026, 1, 16),
            description="مصروف نقدي من العهدة", amount=Decimal("50"),
            expense_account=expense, receipt_number="PC-CF-R1",
            status=PettyCashExpense.Status.SETTLED, settlement=settlement, created_by=user,
        )

        count_entry = JournalEntry.objects.create(
            company=company, branch=branch, fiscal_year=year,
            entry_number="PC-CF-COUNT", entry_date=date(2026, 1, 17),
            description="عجز جرد العهدة",
        )
        JournalLine.objects.create(entry=count_entry, account=expense, debit=Decimal("5"))
        JournalLine.objects.create(entry=count_entry, account=petty_gl, credit=Decimal("5"))
        post_journal(count_entry.pk)
        PettyCashCount.objects.create(
            fund=fund, count_date=date(2026, 1, 17),
            expected_balance=Decimal("150"), actual_balance=Decimal("145"),
            notes="عجز جرد خمسة جنيهات", status=PettyCashCount.Status.APPROVED,
            counted_by=user, reviewed_by=approver, adjustment_journal_entry=count_entry,
        )

        client = APIClient()
        client.force_authenticate(user=user)
        result = client.get(
            f"/api/accounting/journal-entries/direct-cash-flow/?company={company.pk}"
            "&date_from=2026-01-01&date_to=2026-01-31"
        )
        self.assertEqual(result.status_code, 200, result.data)
        operating = next(
            row for row in result.data["sections"]
            if row["category"] == TreasuryTransaction.CashFlowCategory.OPERATING
        )
        self.assertEqual(operating["cash_payments"], Decimal("55.0000"))
        self.assertEqual(operating["net_cash_flow"], Decimal("-55.0000"))
        self.assertEqual(
            {item["source"] for item in operating["details"]},
            {"petty_cash_expense", "petty_cash_count_variance"},
        )
        self.assertEqual(result.data["net_change_in_cash"], Decimal("-55.0000"))
        self.assertEqual(result.data["closing_cash_balance"], Decimal("445.0000"))
        self.assertEqual(result.data["ledger_closing_cash_balance"], Decimal("445.0000"))
        self.assertEqual(result.data["reconciliation_difference"], Decimal("0.0000"))
        self.assertEqual(result.data["closing_balance_difference"], Decimal("0.0000"))

        reverse_operational_journal(
            settlement_entry.pk, user, date(2026, 2, 2),
            expected_source=JournalEntry.Source.MANUAL,
        )
        reverse_operational_journal(
            count_entry.pk, user, date(2026, 2, 3),
            expected_source=JournalEntry.Source.MANUAL,
        )

        january_after_reversals = client.get(
            f"/api/accounting/journal-entries/direct-cash-flow/?company={company.pk}"
            "&date_from=2026-01-01&date_to=2026-01-31"
        )
        self.assertEqual(january_after_reversals.status_code, 200, january_after_reversals.data)
        self.assertEqual(january_after_reversals.data["net_change_in_cash"], Decimal("-55.0000"))
        self.assertEqual(january_after_reversals.data["reconciliation_difference"], Decimal("0.0000"))

        february_reversals = client.get(
            f"/api/accounting/journal-entries/direct-cash-flow/?company={company.pk}"
            "&date_from=2026-02-01&date_to=2026-02-28"
        )
        self.assertEqual(february_reversals.status_code, 200, february_reversals.data)
        feb_operating = next(
            row for row in february_reversals.data["sections"]
            if row["category"] == TreasuryTransaction.CashFlowCategory.OPERATING
        )
        self.assertEqual(feb_operating["cash_receipts"], Decimal("55.0000"))
        self.assertEqual(february_reversals.data["opening_cash_balance"], Decimal("445.0000"))
        self.assertEqual(february_reversals.data["net_change_in_cash"], Decimal("55.0000"))
        self.assertEqual(february_reversals.data["ledger_cash_change"], Decimal("55.0000"))
        self.assertEqual(february_reversals.data["closing_cash_balance"], Decimal("500.0000"))
        self.assertEqual(february_reversals.data["reconciliation_difference"], Decimal("0.0000"))



class AccountingApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.currency = Currency.objects.create(code="EGP", name_ar="جنيه مصري")
        self.company = Company.objects.create(code="API", name_ar="شركة API", country="EG",
            activity="commercial", base_currency=self.currency)
        self.account = ChartOfAccount.objects.create(company=self.company, code="1000",
            name_ar="النقدية", nature="asset")

    def test_account_api_requires_authentication(self):
        response = self.client.get("/api/accounting/accounts/")
        self.assertEqual(response.status_code, 403)

    def test_vat_return_reports_internal_period_and_company_scope(self):
        branch = Branch.objects.create(company=self.company, code="MAIN", name_ar="الرئيسي", is_head_office=True)
        user = User.objects.create_user(username="vat_reporter", password="test-password")
        user.user_permissions.add(Permission.objects.get(codename="view_journalentry"))
        CompanyMembership.objects.create(user=user, company=self.company, role="viewer", all_branches=True)
        self.client.force_authenticate(user=user)
        response = self.client.get(
            f"/api/accounting/journal-entries/vat-return/?company={self.company.pk}&date_from=2026-01-01&date_to=2026-01-31"
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["country"], "EG")
        self.assertEqual(response.data["currency"], "EGP")
        self.assertEqual(response.data["scope_branch_ids"], [branch.pk])
        self.assertEqual(response.data["output_tax"], "0")
        self.assertEqual(response.data["input_tax"], "0")
        self.assertEqual(response.data["net_tax_payable"], "0")
        self.assertEqual(response.data["report_kind"], "internal_vat_reconciliation")

    def test_vat_return_requires_valid_date_range(self):
        user = User.objects.create_user(username="vat_dates", password="test-password")
        user.user_permissions.add(Permission.objects.get(codename="view_journalentry"))
        CompanyMembership.objects.create(user=user, company=self.company, role="viewer", all_branches=True)
        self.client.force_authenticate(user=user)
        response = self.client.get(
            f"/api/accounting/journal-entries/vat-return/?company={self.company.pk}&date_from=2026-02-01&date_to=2026-01-01"
        )
        self.assertEqual(response.status_code, 400)

    def test_trial_balance_separates_opening_and_period_totals_without_double_counting(self):
        branch = Branch.objects.create(company=self.company, code="MAIN", name_ar="الرئيسي", is_head_office=True)
        year = FiscalYear.objects.create(
            company=self.company, name="2026",
            start_date=date(2026, 1, 1), end_date=date(2026, 12, 31),
        )
        capital = ChartOfAccount.objects.create(
            company=self.company, code="3000", name_ar="رأس المال", nature="equity"
        )
        opening = JournalEntry.objects.create(
            company=self.company, branch=branch, fiscal_year=year,
            entry_number="TB-OPEN-001", entry_date=date(2026, 1, 1),
            description="رصيد افتتاحي", source_type=JournalEntry.Source.OPENING,
        )
        JournalLine.objects.create(entry=opening, account=self.account, debit=Decimal("100"))
        JournalLine.objects.create(entry=opening, account=capital, credit=Decimal("100"))
        post_journal(opening.pk)

        movement = JournalEntry.objects.create(
            company=self.company, branch=branch, fiscal_year=year,
            entry_number="TB-MOVE-001", entry_date=date(2026, 1, 15),
            description="حركة قبل الفترة",
        )
        JournalLine.objects.create(entry=movement, account=self.account, debit=Decimal("25"))
        JournalLine.objects.create(entry=movement, account=capital, credit=Decimal("25"))
        post_journal(movement.pk)

        user = User.objects.create_user(username="opening_reporter", password="test-password")
        user.user_permissions.add(Permission.objects.get(codename="view_journalentry"))
        CompanyMembership.objects.create(user=user, company=self.company, role="viewer", all_branches=True)
        self.client.force_authenticate(user=user)

        response = self.client.get(
            f"/api/accounting/journal-entries/trial-balance/?company={self.company.pk}"
            "&date_from=2026-02-01&date_to=2026-02-28"
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["opening_debit_total"], Decimal("125.0000"))
        self.assertEqual(response.data["opening_credit_total"], Decimal("125.0000"))
        self.assertEqual(response.data["debit_total"], Decimal("0.0000"))
        self.assertEqual(response.data["credit_total"], Decimal("0.0000"))
        self.assertEqual(response.data["closing_debit_credit_difference"], Decimal("0.0000"))

    def test_trial_balance_uses_posted_entries_only(self):
        branch = Branch.objects.create(company=self.company, code="MAIN", name_ar="الرئيسي", is_head_office=True)
        year = FiscalYear.objects.create(company=self.company, name="2026",
            start_date=date(2026, 1, 1), end_date=date(2026, 12, 31))
        credit_account = ChartOfAccount.objects.create(company=self.company, code="3000",
            name_ar="رأس المال", nature="equity")
        entry = JournalEntry.objects.create(company=self.company, branch=branch, fiscal_year=year,
            entry_number="TB-001", entry_date=date(2026, 2, 1), description="قيد مرحل")
        JournalLine.objects.create(entry=entry, account=self.account, debit=Decimal("125.00"))
        JournalLine.objects.create(entry=entry, account=credit_account, credit=Decimal("125.00"))
        post_journal(entry.pk)
        draft = JournalEntry.objects.create(company=self.company, branch=branch, fiscal_year=year,
            entry_number="TB-002", entry_date=date(2026, 2, 2), description="مسودة")
        JournalLine.objects.create(entry=draft, account=self.account, debit=Decimal("50.00"))
        JournalLine.objects.create(entry=draft, account=credit_account, credit=Decimal("50.00"))
        user = User.objects.create_user(username="reporter", password="test-password")
        permission = Permission.objects.get(codename="view_journalentry")
        user.user_permissions.add(permission)
        CompanyMembership.objects.create(user=user, company=self.company, role="viewer", all_branches=True)
        self.client.force_authenticate(user=user)
        response = self.client.get(f"/api/accounting/journal-entries/trial-balance/?company={self.company.pk}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["debit_total"], Decimal("125.0000"))
        self.assertEqual(response.data["credit_total"], Decimal("125.0000"))
        self.assertEqual(response.data["difference"], Decimal("0.0000"))

    def test_activity_chart_template_creates_unique_ranges_and_role_links(self):
        company = Company.objects.create(
            code="TPL-COM", name_ar="شركة تجارية", country="EG",
            activity="commercial", base_currency=self.currency,
        )
        user = User.objects.create_user(username="chart_admin", password="test-password")
        CompanyMembership.objects.create(user=user, company=company, role="admin", all_branches=True)
        self.client.force_authenticate(user=user)

        response = self.client.post("/api/accounting/account-templates/", {
            "company": company.pk,
        }, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertGreater(len(response.data["created_accounts"]), 25)
        self.assertEqual(ChartOfAccount.objects.filter(company=company, code="4110").get().name_ar,
                         "إيرادات مبيعات البضائع")
        self.assertEqual(ChartOfAccount.objects.filter(company=company, code="2160").get().nature,
                         ChartOfAccount.Nature.LIABILITY)
        self.assertEqual(ChartOfAccount.objects.filter(company=company, code="1150").get().nature,
                         ChartOfAccount.Nature.ASSET)
        self.assertEqual(AccountRole.objects.get(company=company, role=AccountRole.Role.SALES).account.code, "4110")
        self.assertEqual(AccountRole.objects.get(company=company, role=AccountRole.Role.COGS).account.code, "5110")
        self.assertEqual(AccountRole.objects.get(company=company, role=AccountRole.Role.VAT_INPUT).account.code, "1150")
        self.assertEqual(AccountRole.objects.get(company=company, role=AccountRole.Role.VAT_OUTPUT).account.code, "2120")
        self.assertEqual(AccountRole.objects.get(company=company, role=AccountRole.Role.EMPLOYEE_INSURANCE_PAYABLE).account.code, "2160")
        self.assertEqual(AccountRole.objects.get(company=company, role=AccountRole.Role.EMPLOYER_INSURANCE_PAYABLE).account.code, "2170")
        self.assertEqual(AccountRole.objects.get(company=company, role=AccountRole.Role.REALIZED_FX_GAIN).account.code, "7200")
        self.assertEqual(AccountRole.objects.get(company=company, role=AccountRole.Role.REALIZED_FX_LOSS).account.code, "6150")
        self.assertEqual(AccountRole.objects.get(company=company, role=AccountRole.Role.UNREALIZED_FX_GAIN).account.code, "7210")
        self.assertEqual(AccountRole.objects.get(company=company, role=AccountRole.Role.UNREALIZED_FX_LOSS).account.code, "6160")

    def test_activity_chart_templates_support_configured_primary_activities(self):
        expected = {
            "commercial": ("إيرادات مبيعات البضائع", "تكلفة البضاعة المباعة"),
            "service": ("إيرادات تقديم الخدمات", "تكلفة تقديم الخدمات"),
            "medical": ("إيرادات الخدمات الطبية", "تكلفة الخدمات والمستلزمات الطبية"),
            "restaurants": ("إيرادات المطاعم والمشروبات", "تكلفة الأغذية والمشروبات المباعة"),
            "industrial": ("إيرادات بيع المنتجات", "تكلفة المنتجات المباعة"),
        }
        for activity, (revenue_name, cost_name) in expected.items():
            company = Company.objects.create(
                code=f"TPL-{activity.upper()}", name_ar="شركة نشاط متخصص",
                country="EG", activity=activity, base_currency=self.currency,
            )
            user = User.objects.create_user(
                username=f"template-{activity}", password="test-password"
            )
            CompanyMembership.objects.create(
                user=user, company=company, role="admin", all_branches=True
            )
            self.client.force_authenticate(user=user)
            response = self.client.post(
                "/api/accounting/account-templates/",
                {"company": company.pk}, format="json",
            )
            self.assertEqual(response.status_code, 200, response.data)
            self.assertEqual(
                ChartOfAccount.objects.get(company=company, code="4110").name_ar,
                revenue_name,
            )
            self.assertEqual(
                ChartOfAccount.objects.get(company=company, code="5110").name_ar,
                cost_name,
            )

    def test_activity_chart_template_is_idempotent_and_never_overwrites_existing_role(self):
        company = Company.objects.create(
            code="TPL-SRV", name_ar="شركة خدمات", country="EG",
            activity="service", base_currency=self.currency,
        )
        custom = ChartOfAccount.objects.create(
            company=company, code="9900", name_ar="إيرادات مخصصة", nature="revenue",
        )
        AccountRole.objects.create(company=company, role=AccountRole.Role.SALES, account=custom)
        user = User.objects.create_user(username="chart_admin_2", password="test-password")
        CompanyMembership.objects.create(user=user, company=company, role="admin", all_branches=True)
        self.client.force_authenticate(user=user)

        first = self.client.post("/api/accounting/account-templates/", {"company": company.pk}, format="json")
        self.assertEqual(first.status_code, 200, first.data)
        self.assertEqual(first.data["role_conflicts"][0]["existing_account_code"], "9900")
        self.assertEqual(AccountRole.objects.get(company=company, role=AccountRole.Role.SALES).account_id, custom.pk)
        count = ChartOfAccount.objects.filter(company=company).count()

        second = self.client.post("/api/accounting/account-templates/", {"company": company.pk}, format="json")
        self.assertEqual(second.status_code, 200, second.data)
        self.assertEqual(ChartOfAccount.objects.filter(company=company).count(), count)
        self.assertEqual(ChartOfAccount.objects.get(company=company, code="4110").name_ar, "إيرادات تقديم الخدمات")

    def test_activity_chart_template_stops_on_code_collision_without_partial_creation(self):
        company = Company.objects.create(
            code="TPL-COLL", name_ar="شركة تعارض", country="EG",
            activity="medical", base_currency=self.currency,
        )
        ChartOfAccount.objects.create(
            company=company, code="2120", name_ar="ضريبة القيمة المضافة - مخرجات", nature="liability",
        )
        user = User.objects.create_user(username="chart_admin_3", password="test-password")
        CompanyMembership.objects.create(user=user, company=company, role="admin", all_branches=True)
        self.client.force_authenticate(user=user)

        response = self.client.post("/api/accounting/account-templates/", {"company": company.pk}, format="json")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code_conflicts"][0]["code"], "2120")
        self.assertEqual(ChartOfAccount.objects.filter(company=company).count(), 1)
        self.assertFalse(AccountRole.objects.filter(company=company).exists())

    def test_authenticated_user_can_read_accounts(self):
        user = User.objects.create_user(username="reader", password="test-password")
        permission = Permission.objects.get(codename="view_chartofaccount")
        user.user_permissions.add(permission)
        CompanyMembership.objects.create(user=user, company=self.company, role="viewer")
        self.client.force_authenticate(user=user)
        response = self.client.get("/api/accounting/accounts/?company=" + str(self.company.pk))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 1)

    def test_viewer_cannot_create_account(self):
        user = User.objects.create_user(username="read_only", password="test-password")
        CompanyMembership.objects.create(user=user, company=self.company, role="viewer")
        self.client.force_authenticate(user=user)
        response = self.client.post("/api/accounting/accounts/", {
            "company": self.company.pk,
            "code": "2000",
            "name_ar": "حساب ممنوع",
            "nature": "liability",
        }, format="json")
        self.assertEqual(response.status_code, 403)
        self.assertFalse(ChartOfAccount.objects.filter(company=self.company, code="2000").exists())

    def test_accountant_can_create_account(self):
        user = User.objects.create_user(username="accountant", password="test-password")
        CompanyMembership.objects.create(user=user, company=self.company, role="accountant")
        self.client.force_authenticate(user=user)
        response = self.client.post("/api/accounting/accounts/", {
            "company": self.company.pk,
            "code": "2001",
            "name_ar": "حساب المحاسب",
            "nature": "liability",
        }, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertTrue(ChartOfAccount.objects.filter(company=self.company, code="2001").exists())


class CompanyIsolationApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.currency = Currency.objects.create(code="EGP", name_ar="جنيه مصري")
        self.company_a = Company.objects.create(
            code="A", name_ar="شركة أ", country="EG", activity="commercial", base_currency=self.currency
        )
        self.company_b = Company.objects.create(
            code="B", name_ar="شركة ب", country="EG", activity="service", base_currency=self.currency
        )
        self.account_a = ChartOfAccount.objects.create(
            company=self.company_a, code="1000", name_ar="نقدية أ", nature="asset"
        )
        self.account_b = ChartOfAccount.objects.create(
            company=self.company_b, code="1000", name_ar="نقدية ب", nature="asset"
        )
        self.user = User.objects.create_user(username="tenant_reader", password="test-password")
        self.user.user_permissions.add(Permission.objects.get(codename="view_chartofaccount"))
        CompanyMembership.objects.create(user=self.user, company=self.company_a, role="viewer")
        self.client.force_authenticate(user=self.user)

    def test_account_list_is_scoped_to_membership(self):
        response = self.client.get("/api/accounting/accounts/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["id"] for row in response.data], [self.account_a.pk])

    def test_account_from_unassigned_company_is_not_retrievable(self):
        response = self.client.get(f"/api/accounting/accounts/{self.account_b.pk}/")
        self.assertEqual(response.status_code, 404)


    def test_journal_entries_hide_unassigned_and_inactive_branches(self):
        main = Branch.objects.create(company=self.company_a, code="J-MAIN", name_ar="المركز الرئيسي", is_head_office=True)
        inactive = Branch.objects.create(company=self.company_a, code="J-OLD", name_ar="فرع موقوف", is_active=False)
        year = FiscalYear.objects.create(
            company=self.company_a, name="2029",
            start_date=date(2029, 1, 1), end_date=date(2029, 12, 31),
        )
        visible = JournalEntry.objects.create(
            company=self.company_a, branch=main, fiscal_year=year,
            entry_number="J-MAIN-1", entry_date=date(2029, 1, 2), description="قيد المركز الرئيسي",
        )
        hidden = JournalEntry.objects.create(
            company=self.company_a, branch=inactive, fiscal_year=year,
            entry_number="J-OLD-1", entry_date=date(2029, 1, 2), description="قيد فرع موقوف",
        )
        user = User.objects.create_user(username="journal_branch_reader", password="test-password")
        user.user_permissions.add(Permission.objects.get(codename="view_journalentry"))
        membership = CompanyMembership.objects.create(user=user, company=self.company_a, role="viewer")
        membership.branches.add(main)
        self.client.force_authenticate(user=user)

        response = self.client.get("/api/accounting/journal-entries/")
        self.assertEqual(response.status_code, 200, response.data)
        rows = response.data.get("results", response.data)
        visible_ids = [row["id"] for row in rows]
        self.assertIn(visible.pk, visible_ids)
        self.assertNotIn(hidden.pk, visible_ids)

        hidden_detail = self.client.get(f"/api/accounting/journal-entries/{hidden.pk}/")
        self.assertEqual(hidden_detail.status_code, 404)

    def test_user_cannot_read_an_unassigned_company_trial_balance(self):
        Company.objects.create(
            code="OTHER", name_ar="شركة غير مسموحة", country="SA",
            activity="service", base_currency=self.currency,
        )
        user = User.objects.create_user(username="limited", password="test-password")
        permission = Permission.objects.get(codename="view_journalentry")
        user.user_permissions.add(permission)
        CompanyMembership.objects.create(user=user, company=self.company_a, all_branches=True)
        self.client.force_authenticate(user=user)
        response = self.client.get(f"/api/accounting/journal-entries/trial-balance/?company={self.company_b.pk}")
        self.assertEqual(response.status_code, 403)


    def test_trial_balance_is_limited_to_assigned_branches(self):
        main = Branch.objects.create(company=self.company_a, code="MAIN", name_ar="الرئيسي")
        east = Branch.objects.create(company=self.company_a, code="EAST", name_ar="الشرقي")
        year = FiscalYear.objects.create(
            company=self.company_a, name="2027",
            start_date=date(2027, 1, 1), end_date=date(2027, 12, 31),
        )
        capital = ChartOfAccount.objects.create(
            company=self.company_a, code="3001", name_ar="رأس المال", nature="equity"
        )
        for number, branch in (("MAIN-1", main), ("EAST-1", east)):
            entry = JournalEntry.objects.create(
                company=self.company_a, branch=branch, fiscal_year=year,
                entry_number=number, entry_date=date(2027, 1, 2), description="قيد فرعي",
            )
            JournalLine.objects.create(entry=entry, account=self.account_a, debit=Decimal("100"))
            JournalLine.objects.create(entry=entry, account=capital, credit=Decimal("100"))
            post_journal(entry.pk)
        user = User.objects.create_user(username="branch_reader", password="test-password")
        user.user_permissions.add(Permission.objects.get(codename="view_journalentry"))
        membership = CompanyMembership.objects.create(user=user, company=self.company_a)
        membership.branches.add(main)
        self.client.force_authenticate(user=user)
        response = self.client.get(
            f"/api/accounting/journal-entries/trial-balance/?company={self.company_a.pk}"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["debit_total"], Decimal("100.0000"))
        self.assertEqual(response.data["credit_total"], Decimal("100.0000"))

    def test_trial_balance_branch_filter_returns_only_selected_branch(self):
        main = Branch.objects.create(company=self.company_a, code="TB-MAIN", name_ar="الرئيسي")
        east = Branch.objects.create(company=self.company_a, code="TB-EAST", name_ar="الشرقي")
        year = FiscalYear.objects.create(
            company=self.company_a, name="2028",
            start_date=date(2028, 1, 1), end_date=date(2028, 12, 31),
        )
        capital = ChartOfAccount.objects.create(
            company=self.company_a, code="3002", name_ar="رأس المال", nature="equity"
        )
        for number, branch in (("TB-MAIN-1", main), ("TB-EAST-1", east)):
            entry = JournalEntry.objects.create(
                company=self.company_a, branch=branch, fiscal_year=year,
                entry_number=number, entry_date=date(2028, 1, 2), description="قيد فرعي",
            )
            JournalLine.objects.create(entry=entry, account=self.account_a, debit=Decimal("100"))
            JournalLine.objects.create(entry=entry, account=capital, credit=Decimal("100"))
            post_journal(entry.pk)
        user = User.objects.create_user(username="tb_branch_filter", password="test-password")
        user.user_permissions.add(Permission.objects.get(codename="view_journalentry"))
        CompanyMembership.objects.create(user=user, company=self.company_a, role="viewer", all_branches=True)
        self.client.force_authenticate(user=user)

        response = self.client.get(
            f"/api/accounting/journal-entries/trial-balance/?company={self.company_a.pk}&branch={east.pk}"
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["debit_total"], Decimal("100.0000"))
        self.assertEqual(response.data["credit_total"], Decimal("100.0000"))


class IndirectCashFlowApiTests(TestCase):
    def test_indirect_cash_flow_reconciles_to_cash_ledger_and_uses_opening_balances(self):
        currency = Currency.objects.create(code="EGP", name_ar="جنيه مصري")
        company = Company.objects.create(code="CF", name_ar="شركة التدفقات", country="EG",
            activity="commercial", base_currency=currency)
        branch = Branch.objects.create(company=company, code="MAIN", name_ar="الرئيسي", is_head_office=True)
        Branch.objects.create(company=company, code="SEC", name_ar="الفرع الثاني")
        year = FiscalYear.objects.create(company=company, name="2026",
            start_date=date(2026, 1, 1), end_date=date(2026, 12, 31))
        cash = ChartOfAccount.objects.create(company=company, code="1000", name_ar="النقدية", nature="asset")
        receivable = ChartOfAccount.objects.create(company=company, code="1100", name_ar="العملاء", nature="asset")
        payable = ChartOfAccount.objects.create(company=company, code="2100", name_ar="الموردون", nature="liability")
        payroll_payable = ChartOfAccount.objects.create(company=company, code="2110", name_ar="مرتبات مستحقة", nature="liability")
        grni = ChartOfAccount.objects.create(company=company, code="2120", name_ar="بضاعة مستلمة غير مفوترة", nature="liability")
        payroll_tax = ChartOfAccount.objects.create(company=company, code="2130", name_ar="ضريبة مرتبات مستحقة", nature="liability")
        fx_gain = ChartOfAccount.objects.create(company=company, code="4100", name_ar="أرباح فروق عملة غير محققة", nature="revenue")
        fx_loss = ChartOfAccount.objects.create(company=company, code="5101", name_ar="خسائر فروق عملة غير محققة", nature="expense")
        capital = ChartOfAccount.objects.create(company=company, code="3000", name_ar="رأس المال", nature="equity")
        revenue = ChartOfAccount.objects.create(company=company, code="4000", name_ar="المبيعات", nature="revenue")
        expense = ChartOfAccount.objects.create(company=company, code="5000", name_ar="مصروفات", nature="expense")
        AccountRole.objects.create(company=company, role=AccountRole.Role.ACCOUNTS_RECEIVABLE, account=receivable)
        AccountRole.objects.create(company=company, role=AccountRole.Role.ACCOUNTS_PAYABLE, account=payable)
        AccountRole.objects.create(company=company, role=AccountRole.Role.PAYROLL_PAYABLE, account=payroll_payable)
        AccountRole.objects.create(company=company, role=AccountRole.Role.PAYROLL_TAX_PAYABLE, account=payroll_tax)
        AccountRole.objects.create(company=company, role=AccountRole.Role.GOODS_RECEIVED_NOT_INVOICED, account=grni)
        AccountRole.objects.create(company=company, role=AccountRole.Role.UNREALIZED_FX_GAIN, account=fx_gain)
        AccountRole.objects.create(company=company, role=AccountRole.Role.UNREALIZED_FX_LOSS, account=fx_loss)
        from treasury.models import TreasuryAccount
        TreasuryAccount.objects.create(company=company, account=cash, kind="cash", code="CASH",
            name_ar="الخزينة الرئيسية")

        opening = JournalEntry.objects.create(company=company, branch=branch, fiscal_year=year,
            entry_number="OPEN-1", entry_date=date(2026, 1, 1), description="أرصدة افتتاحية",
            source_type=JournalEntry.Source.OPENING)
        JournalLine.objects.create(entry=opening, account=cash, debit=Decimal("500"))
        JournalLine.objects.create(entry=opening, account=receivable, debit=Decimal("100"))
        JournalLine.objects.create(entry=opening, account=payable, credit=Decimal("50"))
        JournalLine.objects.create(entry=opening, account=capital, credit=Decimal("550"))
        post_journal(opening.pk)

        sales = JournalEntry.objects.create(company=company, branch=branch, fiscal_year=year,
            entry_number="SALE-1", entry_date=date(2026, 1, 2), description="مبيعات")
        JournalLine.objects.create(entry=sales, account=cash, debit=Decimal("600"))
        JournalLine.objects.create(entry=sales, account=receivable, debit=Decimal("400"))
        JournalLine.objects.create(entry=sales, account=revenue, credit=Decimal("1000"))
        post_journal(sales.pk)

        purchase_expense = JournalEntry.objects.create(company=company, branch=branch, fiscal_year=year,
            entry_number="EXP-1", entry_date=date(2026, 1, 3), description="مصروف آجل")
        JournalLine.objects.create(entry=purchase_expense, account=expense, debit=Decimal("300"))
        JournalLine.objects.create(entry=purchase_expense, account=payable, credit=Decimal("300"))
        post_journal(purchase_expense.pk)

        payroll_accrual = JournalEntry.objects.create(company=company, branch=branch, fiscal_year=year,
            entry_number="PAYROLL-ACCRUAL-1", entry_date=date(2026, 1, 4), description="استحقاق مرتبات")
        JournalLine.objects.create(entry=payroll_accrual, account=expense, debit=Decimal("200"))
        JournalLine.objects.create(entry=payroll_accrual, account=payroll_payable, credit=Decimal("200"))
        post_journal(payroll_accrual.pk)

        grni_accrual = JournalEntry.objects.create(company=company, branch=branch, fiscal_year=year,
            entry_number="GRNI-1", entry_date=date(2026, 1, 5), description="بضاعة مستلمة غير مفوترة")
        JournalLine.objects.create(entry=grni_accrual, account=expense, debit=Decimal("20"))
        JournalLine.objects.create(entry=grni_accrual, account=grni, credit=Decimal("20"))
        post_journal(grni_accrual.pk)

        payroll_tax_accrual = JournalEntry.objects.create(company=company, branch=branch, fiscal_year=year,
            entry_number="PAYROLL-TAX-1", entry_date=date(2026, 1, 6), description="استحقاق ضريبة مرتبات")
        JournalLine.objects.create(entry=payroll_tax_accrual, account=expense, debit=Decimal("30"))
        JournalLine.objects.create(entry=payroll_tax_accrual, account=payroll_tax, credit=Decimal("30"))
        post_journal(payroll_tax_accrual.pk)

        fx_gain_entry = JournalEntry.objects.create(company=company, branch=branch, fiscal_year=year,
            entry_number="FX-GAIN-1", entry_date=date(2026, 1, 7), description="فرق عملة غير محقق - ربح")
        JournalLine.objects.create(entry=fx_gain_entry, account=receivable, debit=Decimal("40"))
        JournalLine.objects.create(entry=fx_gain_entry, account=fx_gain, credit=Decimal("40"))
        post_journal(fx_gain_entry.pk)

        fx_loss_entry = JournalEntry.objects.create(company=company, branch=branch, fiscal_year=year,
            entry_number="FX-LOSS-1", entry_date=date(2026, 1, 8), description="فرق عملة غير محقق - خسارة")
        JournalLine.objects.create(entry=fx_loss_entry, account=fx_loss, debit=Decimal("25"))
        JournalLine.objects.create(entry=fx_loss_entry, account=payable, credit=Decimal("25"))
        post_journal(fx_loss_entry.pk)

        user = User.objects.create_user(username="cashflow_reader", password="test-password")
        user.user_permissions.add(Permission.objects.get(codename="view_journalentry"))
        CompanyMembership.objects.create(user=user, company=company, role="viewer", all_branches=True)
        client = APIClient()
        client.force_authenticate(user=user)
        response = client.get(
            f"/api/accounting/journal-entries/indirect-cash-flow/?company={company.pk}"
            "&date_from=2026-01-01&date_to=2026-12-31"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["net_profit"], Decimal("465.0000"))
        self.assertEqual(response.data["operating_adjustments"], Decimal("150.0000"))
        self.assertEqual(response.data["non_cash_adjustments"]["unrealized_fx_gain"], Decimal("40.0000"))
        self.assertEqual(response.data["non_cash_adjustments"]["unrealized_fx_loss"], Decimal("25.0000"))
        self.assertEqual(response.data["non_cash_adjustments"]["unrealized_fx_net_adjustment"], Decimal("-15.0000"))
        self.assertEqual(response.data["operating_cash_flow"], Decimal("600.0000"))
        self.assertEqual(response.data["ledger_cash_change"], Decimal("600.0000"))
        self.assertEqual(response.data["reconciliation_difference"], Decimal("0.0000"))


    def test_cash_flow_and_equity_use_branch_specific_opening_snapshots(self):
        currency = Currency.objects.create(code="EGP", name_ar="جنيه مصري")
        company = Company.objects.create(
            code="BR-CF-EQ", name_ar="شركة افتتاحيات الفروع",
            country="EG", activity="commercial", base_currency=currency,
        )
        snapshot_branch = Branch.objects.create(
            company=company, code="SNAP", name_ar="فرع افتتاحيه", is_head_office=True,
        )
        carry_branch = Branch.objects.create(
            company=company, code="CARRY", name_ar="فرع مرحل",
        )
        prior_year = FiscalYear.objects.create(
            company=company, name="2025",
            start_date=date(2025, 1, 1), end_date=date(2025, 12, 31),
        )
        current_year = FiscalYear.objects.create(
            company=company, name="2026",
            start_date=date(2026, 1, 1), end_date=date(2026, 12, 31),
        )
        cash = ChartOfAccount.objects.create(
            company=company, code="1000", name_ar="النقدية", nature=ChartOfAccount.Nature.ASSET,
        )
        receivable = ChartOfAccount.objects.create(
            company=company, code="1100", name_ar="العملاء", nature=ChartOfAccount.Nature.ASSET,
        )
        capital = ChartOfAccount.objects.create(
            company=company, code="3000", name_ar="رأس المال", nature=ChartOfAccount.Nature.EQUITY,
        )
        AccountRole.objects.create(
            company=company, role=AccountRole.Role.ACCOUNTS_RECEIVABLE, account=receivable,
        )
        TreasuryAccount.objects.create(
            company=company, account=cash, kind="cash", code="CASH",
            name_ar="الخزينة الرئيسية",
        )

        prior_entry = JournalEntry.objects.create(
            company=company, branch=carry_branch, fiscal_year=prior_year,
            entry_number="BR-CARRY-OPEN", entry_date=date(2025, 12, 31),
            description="أرصدة الفرع المرحل",
        )
        JournalLine.objects.create(entry=prior_entry, account=cash, debit=Decimal("50"))
        JournalLine.objects.create(entry=prior_entry, account=receivable, debit=Decimal("25"))
        JournalLine.objects.create(entry=prior_entry, account=capital, credit=Decimal("75"))
        post_journal(prior_entry.pk)

        prior_snapshot_entry = JournalEntry.objects.create(
            company=company, branch=snapshot_branch, fiscal_year=prior_year,
            entry_number="BR-SNAPSHOT-PRIOR", entry_date=date(2025, 12, 31),
            description="أرصدة سابقة قبل لقطة الفرع",
        )
        JournalLine.objects.create(entry=prior_snapshot_entry, account=cash, debit=Decimal("900"))
        JournalLine.objects.create(entry=prior_snapshot_entry, account=receivable, debit=Decimal("100"))
        JournalLine.objects.create(entry=prior_snapshot_entry, account=capital, credit=Decimal("1000"))
        post_journal(prior_snapshot_entry.pk)

        opening_entry = JournalEntry.objects.create(
            company=company, branch=snapshot_branch, fiscal_year=current_year,
            entry_number="BR-SNAPSHOT-OPEN", entry_date=date(2026, 1, 1),
            description="أرصدة افتتاحية للفرع", source_type=JournalEntry.Source.OPENING,
        )
        JournalLine.objects.create(entry=opening_entry, account=cash, debit=Decimal("70"))
        JournalLine.objects.create(entry=opening_entry, account=receivable, debit=Decimal("30"))
        JournalLine.objects.create(entry=opening_entry, account=capital, credit=Decimal("100"))
        post_journal(opening_entry.pk)

        user = User.objects.create_user(username="branch_statement_reader", password="test-password")
        user.user_permissions.add(Permission.objects.get(codename="view_journalentry"))
        CompanyMembership.objects.create(user=user, company=company, role="viewer", all_branches=True)
        client = APIClient()
        client.force_authenticate(user=user)

        cash_flow = client.get(
            f"/api/accounting/journal-entries/indirect-cash-flow/?company={company.pk}"
            "&date_from=2026-01-01&date_to=2026-01-31"
        )
        direct_cash_flow = client.get(
            f"/api/accounting/journal-entries/direct-cash-flow/?company={company.pk}"
            "&date_from=2026-01-01&date_to=2026-01-31"
        )
        equity = client.get(
            f"/api/accounting/journal-entries/changes-in-equity/?company={company.pk}"
            "&date_from=2026-01-01&date_to=2026-01-31"
        )

        self.assertEqual(cash_flow.status_code, 200, cash_flow.data)
        self.assertEqual(direct_cash_flow.status_code, 200, direct_cash_flow.data)
        self.assertEqual(equity.status_code, 200, equity.data)
        self.assertEqual(cash_flow.data["opening_cash_balance"], Decimal("120.0000"))
        self.assertEqual(cash_flow.data["closing_cash_balance"], Decimal("120.0000"))
        self.assertEqual(cash_flow.data["ledger_closing_cash_balance"], Decimal("120.0000"))
        self.assertEqual(cash_flow.data["reconciliation_difference"], Decimal("0.0000"))
        self.assertEqual(direct_cash_flow.data["opening_cash_balance"], Decimal("120.0000"))
        self.assertEqual(direct_cash_flow.data["closing_cash_balance"], Decimal("120.0000"))
        self.assertEqual(direct_cash_flow.data["ledger_closing_cash_balance"], Decimal("120.0000"))
        self.assertEqual(direct_cash_flow.data["reconciliation_difference"], Decimal("0.0000"))
        receivable_row = next(
            row for row in cash_flow.data["working_capital"]
            if row["role"] == AccountRole.Role.ACCOUNTS_RECEIVABLE
        )
        self.assertEqual(receivable_row["opening_balance"], Decimal("55.0000"))
        self.assertEqual(receivable_row["closing_balance"], Decimal("55.0000"))
        self.assertEqual(receivable_row["cash_flow_adjustment"], Decimal("0.0000"))
        self.assertEqual(cash_flow.data["reconciliation_difference"], Decimal("0.0000"))
        self.assertEqual(equity.data["opening_equity"], Decimal("175.0000"))
        self.assertEqual(equity.data["period_net_profit"], Decimal("0.0000"))
        self.assertEqual(equity.data["closing_equity"], Decimal("175.0000"))


class FinancialStatementsApiTests(TestCase):
    def test_balance_sheet_includes_unclosed_profit_and_income_statement_excludes_opening(self):
        currency = Currency.objects.create(code="EGP", name_ar="جنيه مصري")
        company = Company.objects.create(code="FS", name_ar="شركة القوائم", country="EG",
            activity="commercial", base_currency=currency)
        branch = Branch.objects.create(company=company, code="MAIN", name_ar="الرئيسي", is_head_office=True)
        second_branch = Branch.objects.create(company=company, code="SEC", name_ar="الفرع الثاني")
        year = FiscalYear.objects.create(company=company, name="2026",
            start_date=date(2026, 1, 1), end_date=date(2026, 12, 31))
        cash = ChartOfAccount.objects.create(company=company, code="1000", name_ar="النقدية", nature="asset")
        capital = ChartOfAccount.objects.create(company=company, code="3000", name_ar="رأس المال", nature="equity")
        revenue = ChartOfAccount.objects.create(company=company, code="4000", name_ar="الإيرادات", nature="revenue")
        expense = ChartOfAccount.objects.create(company=company, code="5000", name_ar="المصروفات", nature="expense")
        opening = JournalEntry.objects.create(company=company, branch=branch, fiscal_year=year,
            entry_number="OPEN-FS", entry_date=date(2026, 1, 1), description="افتتاحي",
            source_type=JournalEntry.Source.OPENING)
        JournalLine.objects.create(entry=opening, account=cash, debit=Decimal("100"))
        JournalLine.objects.create(entry=opening, account=capital, credit=Decimal("100"))
        post_journal(opening.pk)
        operations = JournalEntry.objects.create(company=company, branch=branch, fiscal_year=year,
            entry_number="OPS-FS", entry_date=date(2026, 1, 2), description="تشغيل")
        JournalLine.objects.create(entry=operations, account=cash, debit=Decimal("200"))
        JournalLine.objects.create(entry=operations, account=revenue, credit=Decimal("200"))
        JournalLine.objects.create(entry=operations, account=expense, debit=Decimal("50"))
        JournalLine.objects.create(entry=operations, account=cash, credit=Decimal("50"))
        post_journal(operations.pk)
        second_operations = JournalEntry.objects.create(company=company, branch=second_branch, fiscal_year=year,
            entry_number="OPS-FS-2", entry_date=date(2026, 1, 3), description="تشغيل الفرع الثاني")
        JournalLine.objects.create(entry=second_operations, account=cash, debit=Decimal("50"))
        JournalLine.objects.create(entry=second_operations, account=revenue, credit=Decimal("50"))
        JournalLine.objects.create(entry=second_operations, account=expense, debit=Decimal("50"))
        JournalLine.objects.create(entry=second_operations, account=cash, credit=Decimal("50"))
        post_journal(second_operations.pk)

        user = User.objects.create_user(username="financial_reader", password="test-password")
        user.user_permissions.add(Permission.objects.get(codename="view_journalentry"))
        CompanyMembership.objects.create(user=user, company=company, role="viewer", all_branches=True)
        client = APIClient()
        client.force_authenticate(user=user)
        balance_response = client.get(
            f"/api/accounting/journal-entries/balance-sheet/?company={company.pk}&as_of=2026-01-31"
        )
        income_response = client.get(
            f"/api/accounting/journal-entries/income-statement/?company={company.pk}"
            "&date_from=2026-01-01&date_to=2026-01-31"
        )
        ledger_response = client.get(
            f"/api/accounting/journal-entries/general-ledger/?company={company.pk}"
            f"&account={cash.pk}&date_from=2026-01-01&date_to=2026-01-31"
        )
        journal_detail_response = client.get(
            f"/api/accounting/journal-entries/{operations.pk}/"
        )
        equity_response = client.get(
            f"/api/accounting/journal-entries/changes-in-equity/?company={company.pk}"
            "&date_from=2026-01-01&date_to=2026-01-31"
        )
        branch_income_response = client.get(
            f"/api/accounting/journal-entries/income-statement/?company={company.pk}&branch={branch.pk}"
            "&date_from=2026-01-01&date_to=2026-01-31"
        )
        branch_ledger_response = client.get(
            f"/api/accounting/journal-entries/general-ledger/?company={company.pk}&branch={branch.pk}"
            f"&account={cash.pk}&date_from=2026-01-01&date_to=2026-01-31"
        )
        self.assertEqual(balance_response.status_code, 200)
        self.assertEqual(income_response.status_code, 200)
        self.assertEqual(ledger_response.status_code, 200)
        self.assertEqual(journal_detail_response.status_code, 200)
        self.assertEqual(journal_detail_response.data["entry_number"], "OPS-FS")
        self.assertEqual(len(journal_detail_response.data["lines"]), 4)
        self.assertEqual(equity_response.status_code, 200)
        self.assertEqual(branch_income_response.status_code, 200)
        self.assertEqual(branch_ledger_response.status_code, 200)
        self.assertEqual(branch_income_response.data["revenue_total"], Decimal("200.0000"))
        self.assertEqual(branch_income_response.data["expenses_total"], Decimal("50.0000"))
        self.assertEqual(branch_ledger_response.data["closing_balance"], Decimal("250.0000"))
        self.assertEqual(balance_response.data["total_assets"], Decimal("250.0000"))
        self.assertEqual(balance_response.data["total_equity"], Decimal("250.0000"))
        self.assertEqual(balance_response.data["difference"], Decimal("0.0000"))
        self.assertEqual(income_response.data["revenue_total"], Decimal("250.0000"))
        self.assertEqual(income_response.data["expenses_total"], Decimal("100.0000"))
        self.assertEqual(income_response.data["net_profit"], Decimal("150.0000"))
        self.assertEqual(ledger_response.data["opening_balance"], Decimal("100.0000"))
        self.assertEqual(ledger_response.data["closing_balance"], Decimal("250.0000"))
        self.assertEqual(equity_response.data["opening_equity"], Decimal("100.0000"))
        self.assertEqual(equity_response.data["period_net_profit"], Decimal("150.0000"))
        self.assertEqual(equity_response.data["closing_equity"], Decimal("250.0000"))


    def test_balance_sheet_carries_forward_only_branches_without_opening_snapshots(self):
        currency = Currency.objects.create(code="EGP", name_ar="جنيه مصري")
        company = Company.objects.create(
            code="BR-BAL", name_ar="شركة ميزانية الفروع", country="EG",
            activity="commercial", base_currency=currency,
        )
        snapshot_branch = Branch.objects.create(
            company=company, code="SNAP", name_ar="فرع له افتتاحي", is_head_office=True,
        )
        carry_branch = Branch.objects.create(
            company=company, code="CARRY", name_ar="فرع بدون افتتاحي",
        )
        prior_year = FiscalYear.objects.create(
            company=company, name="2025",
            start_date=date(2025, 1, 1), end_date=date(2025, 12, 31),
        )
        current_year = FiscalYear.objects.create(
            company=company, name="2026",
            start_date=date(2026, 1, 1), end_date=date(2026, 12, 31),
        )
        cash = ChartOfAccount.objects.create(
            company=company, code="1000", name_ar="النقدية", nature=ChartOfAccount.Nature.ASSET,
        )
        capital = ChartOfAccount.objects.create(
            company=company, code="3000", name_ar="رأس المال", nature=ChartOfAccount.Nature.EQUITY,
        )

        prior_entry = JournalEntry.objects.create(
            company=company, branch=carry_branch, fiscal_year=prior_year,
            entry_number="BR-PRIOR-001", entry_date=date(2025, 12, 31),
            description="رصيد مرحل للفرع دون افتتاحي",
        )
        JournalLine.objects.create(entry=prior_entry, account=cash, debit=Decimal("75"))
        JournalLine.objects.create(entry=prior_entry, account=capital, credit=Decimal("75"))
        post_journal(prior_entry.pk)

        opening_entry = JournalEntry.objects.create(
            company=company, branch=snapshot_branch, fiscal_year=current_year,
            entry_number="BR-OPEN-001", entry_date=date(2026, 1, 1),
            description="افتتاحي الفرع الأول", source_type=JournalEntry.Source.OPENING,
        )
        JournalLine.objects.create(entry=opening_entry, account=cash, debit=Decimal("100"))
        JournalLine.objects.create(entry=opening_entry, account=capital, credit=Decimal("100"))
        post_journal(opening_entry.pk)

        user = User.objects.create_user(username="branch_balance_reader", password="test-password")
        user.user_permissions.add(Permission.objects.get(codename="view_journalentry"))
        CompanyMembership.objects.create(user=user, company=company, role="viewer", all_branches=True)
        client = APIClient()
        client.force_authenticate(user=user)

        result = client.get(
            f"/api/accounting/journal-entries/balance-sheet/?company={company.pk}&as_of=2026-01-31"
        )

        self.assertEqual(result.status_code, 200, result.data)
        self.assertEqual(result.data["total_assets"], Decimal("175.0000"))
        self.assertEqual(result.data["total_equity"], Decimal("175.0000"))
        self.assertEqual(result.data["difference"], Decimal("0.0000"))

    def test_prior_unclosed_profit_carries_into_balance_sheet_and_equity(self):
        currency = Currency.objects.create(code="EGP", name_ar="جنيه مصري")
        company = Company.objects.create(
            code="PRIOR-PL", name_ar="شركة أرباح سنوات سابقة",
            country="EG", activity="commercial", base_currency=currency,
        )
        branch = Branch.objects.create(
            company=company, code="MAIN", name_ar="الرئيسي", is_head_office=True,
        )
        prior_year = FiscalYear.objects.create(
            company=company, name="2025",
            start_date=date(2025, 1, 1), end_date=date(2025, 12, 31),
        )
        FiscalYear.objects.create(
            company=company, name="2026",
            start_date=date(2026, 1, 1), end_date=date(2026, 12, 31),
        )
        cash = ChartOfAccount.objects.create(
            company=company, code="1000", name_ar="النقدية",
            nature=ChartOfAccount.Nature.ASSET,
        )
        revenue = ChartOfAccount.objects.create(
            company=company, code="4000", name_ar="الإيرادات",
            nature=ChartOfAccount.Nature.REVENUE,
        )
        expense = ChartOfAccount.objects.create(
            company=company, code="5000", name_ar="المصروفات",
            nature=ChartOfAccount.Nature.EXPENSE,
        )
        prior_operations = JournalEntry.objects.create(
            company=company, branch=branch, fiscal_year=prior_year,
            entry_number="PRIOR-OPS-001", entry_date=date(2025, 12, 20),
            description="إيرادات ومصروفات سنة سابقة لم تقفل",
        )
        JournalLine.objects.create(
            entry=prior_operations, account=cash, debit=Decimal("200"),
        )
        JournalLine.objects.create(
            entry=prior_operations, account=revenue, credit=Decimal("200"),
        )
        JournalLine.objects.create(
            entry=prior_operations, account=expense, debit=Decimal("50"),
        )
        JournalLine.objects.create(
            entry=prior_operations, account=cash, credit=Decimal("50"),
        )
        post_journal(prior_operations.pk)

        user = User.objects.create_user(username="prior_profit_reader", password="test-password")
        user.user_permissions.add(Permission.objects.get(codename="view_journalentry"))
        CompanyMembership.objects.create(
            user=user, company=company, role="viewer", all_branches=True,
        )
        client = APIClient()
        client.force_authenticate(user=user)

        balance = client.get(
            f"/api/accounting/journal-entries/balance-sheet/?company={company.pk}&as_of=2026-01-31"
        )
        equity = client.get(
            f"/api/accounting/journal-entries/changes-in-equity/?company={company.pk}"
            "&date_from=2026-01-01&date_to=2026-01-31"
        )

        self.assertEqual(balance.status_code, 200, balance.data)
        self.assertEqual(equity.status_code, 200, equity.data)
        self.assertEqual(balance.data["total_assets"], Decimal("150.0000"))
        self.assertEqual(balance.data["prior_unclosed_profit"], Decimal("150.0000"))
        self.assertEqual(balance.data["total_equity"], Decimal("150.0000"))
        self.assertEqual(balance.data["difference"], Decimal("0.0000"))
        self.assertEqual(equity.data["opening_equity"], Decimal("150.0000"))
        self.assertEqual(equity.data["period_net_profit"], Decimal("0.0000"))
        self.assertEqual(equity.data["closing_equity"], Decimal("150.0000"))
        self.assertTrue(any(
            row["code"] == "PRIOR-UNCLOSED-PROFIT" for row in equity.data["accounts"]
        ))


    def test_balance_sheet_does_not_double_count_profit_already_closed_to_equity(self):
        currency = Currency.objects.create(code="EGP", name_ar="جنيه مصري")
        company = Company.objects.create(
            code="CLOSED-PL", name_ar="شركة نتيجة مقفلة",
            country="EG", activity="commercial", base_currency=currency,
        )
        branch = Branch.objects.create(
            company=company, code="MAIN", name_ar="الرئيسي", is_head_office=True,
        )
        year = FiscalYear.objects.create(
            company=company, name="2026",
            start_date=date(2026, 1, 1), end_date=date(2026, 12, 31),
        )
        cash = ChartOfAccount.objects.create(
            company=company, code="1000", name_ar="النقدية",
            nature=ChartOfAccount.Nature.ASSET,
        )
        retained = ChartOfAccount.objects.create(
            company=company, code="3000", name_ar="الأرباح المرحلة",
            nature=ChartOfAccount.Nature.EQUITY,
        )
        revenue = ChartOfAccount.objects.create(
            company=company, code="4000", name_ar="الإيرادات",
            nature=ChartOfAccount.Nature.REVENUE,
        )
        expense = ChartOfAccount.objects.create(
            company=company, code="5000", name_ar="المصروفات",
            nature=ChartOfAccount.Nature.EXPENSE,
        )
        operations = JournalEntry.objects.create(
            company=company, branch=branch, fiscal_year=year,
            entry_number="CLOSED-OPS-001", entry_date=date(2026, 12, 20),
            description="نتيجة النشاط قبل الإقفال",
        )
        JournalLine.objects.create(entry=operations, account=cash, debit=Decimal("200"))
        JournalLine.objects.create(entry=operations, account=revenue, credit=Decimal("200"))
        JournalLine.objects.create(entry=operations, account=expense, debit=Decimal("50"))
        JournalLine.objects.create(entry=operations, account=cash, credit=Decimal("50"))
        post_journal(operations.pk)

        closing = JournalEntry.objects.create(
            company=company, branch=branch, fiscal_year=year,
            entry_number="CLOSED-CLOSE-001", entry_date=date(2026, 12, 31),
            description="إقفال نتيجة الأعمال", source_type=JournalEntry.Source.CLOSING,
        )
        JournalLine.objects.create(entry=closing, account=revenue, debit=Decimal("200"))
        JournalLine.objects.create(entry=closing, account=expense, credit=Decimal("50"))
        JournalLine.objects.create(entry=closing, account=retained, credit=Decimal("150"))
        post_journal(closing.pk)

        user = User.objects.create_user(username="closed_profit_reader", password="test-password")
        user.user_permissions.add(Permission.objects.get(codename="view_journalentry"))
        CompanyMembership.objects.create(
            user=user, company=company, role="viewer", all_branches=True,
        )
        client = APIClient()
        client.force_authenticate(user=user)
        result = client.get(
            f"/api/accounting/journal-entries/balance-sheet/?company={company.pk}&as_of=2026-12-31"
        )

        self.assertEqual(result.status_code, 200, result.data)
        self.assertEqual(result.data["total_assets"], Decimal("150.0000"))
        self.assertEqual(result.data["current_year_profit"], Decimal("0.0000"))
        self.assertEqual(result.data["total_equity"], Decimal("150.0000"))
        self.assertEqual(result.data["difference"], Decimal("0.0000"))


    def test_income_statement_separates_cost_of_sales_from_operating_expenses(self):
        currency = Currency.objects.create(code="EGP", name_ar="جنيه مصري")
        company = Company.objects.create(
            code="COGS-FS", name_ar="شركة اختبار تكلفة المبيعات", country="EG",
            activity="commercial", base_currency=currency,
        )
        branch = Branch.objects.create(
            company=company, code="MAIN", name_ar="الرئيسي", is_head_office=True,
        )
        year = FiscalYear.objects.create(
            company=company, name="2026",
            start_date=date(2026, 1, 1), end_date=date(2026, 12, 31),
        )
        cash = ChartOfAccount.objects.create(
            company=company, code="1000", name_ar="النقدية", nature="asset",
        )
        inventory = ChartOfAccount.objects.create(
            company=company, code="1200", name_ar="المخزون", nature="asset",
        )
        revenue = ChartOfAccount.objects.create(
            company=company, code="4100", name_ar="المبيعات", nature="revenue",
        )
        cogs = ChartOfAccount.objects.create(
            company=company, code="5100", name_ar="تكلفة المبيعات", nature="expense",
        )
        operating = ChartOfAccount.objects.create(
            company=company, code="5200", name_ar="مصروفات تشغيلية", nature="expense",
        )
        AccountRole.objects.create(
            company=company, role=AccountRole.Role.COGS, account=cogs,
        )

        sale = JournalEntry.objects.create(
            company=company, branch=branch, fiscal_year=year,
            entry_number="SALE-COGS-1", entry_date=date(2026, 1, 2),
            description="إثبات المبيعات",
        )
        JournalLine.objects.create(entry=sale, account=cash, debit=Decimal("1000"))
        JournalLine.objects.create(entry=sale, account=revenue, credit=Decimal("1000"))
        post_journal(sale.pk)

        cost = JournalEntry.objects.create(
            company=company, branch=branch, fiscal_year=year,
            entry_number="COGS-1", entry_date=date(2026, 1, 2),
            description="إثبات تكلفة البضاعة المباعة",
        )
        JournalLine.objects.create(entry=cost, account=cogs, debit=Decimal("400"))
        JournalLine.objects.create(entry=cost, account=inventory, credit=Decimal("400"))
        post_journal(cost.pk)

        expense = JournalEntry.objects.create(
            company=company, branch=branch, fiscal_year=year,
            entry_number="OPEX-1", entry_date=date(2026, 1, 3),
            description="مصروف تشغيلي",
        )
        JournalLine.objects.create(entry=expense, account=operating, debit=Decimal("150"))
        JournalLine.objects.create(entry=expense, account=cash, credit=Decimal("150"))
        post_journal(expense.pk)

        user = User.objects.create_user(username="cogs_report_reader", password="test-password")
        user.user_permissions.add(Permission.objects.get(codename="view_journalentry"))
        CompanyMembership.objects.create(
            user=user, company=company, role="viewer", all_branches=True,
        )
        client = APIClient()
        client.force_authenticate(user=user)
        response = client.get(
            f"/api/accounting/journal-entries/income-statement/?company={company.pk}"
            "&date_from=2026-01-01&date_to=2026-01-31"
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["revenue_total"], Decimal("1000.0000"))
        self.assertEqual(response.data["cost_of_sales_total"], Decimal("400.0000"))
        self.assertEqual(response.data["gross_profit"], Decimal("600.0000"))
        self.assertEqual(response.data["expenses_total"], Decimal("150.0000"))
        self.assertEqual(response.data["net_profit"], Decimal("450.0000"))
        self.assertEqual([row["account_id"] for row in response.data["cost_of_sales"]], [cogs.pk])
        self.assertEqual([row["account_id"] for row in response.data["operating_and_other_expenses"]], [operating.pk])


    def test_new_fiscal_year_opening_balances_do_not_duplicate_prior_year_balances(self):
        currency = Currency.objects.create(code="EGP", name_ar="جنيه مصري")
        company = Company.objects.create(
            code="OPENING-ROLL", name_ar="شركة اختبار ترحيل الأرصدة",
            country="EG", activity="commercial", base_currency=currency,
        )
        branch = Branch.objects.create(
            company=company, code="MAIN", name_ar="الرئيسي", is_head_office=True,
        )
        year_2026 = FiscalYear.objects.create(
            company=company, name="2026",
            start_date=date(2026, 1, 1), end_date=date(2026, 12, 31),
        )
        year_2027 = FiscalYear.objects.create(
            company=company, name="2027",
            start_date=date(2027, 1, 1), end_date=date(2027, 12, 31),
        )
        cash = ChartOfAccount.objects.create(
            company=company, code="1000", name_ar="النقدية", nature="asset",
        )
        capital = ChartOfAccount.objects.create(
            company=company, code="3000", name_ar="رأس المال", nature="equity",
        )
        revenue = ChartOfAccount.objects.create(
            company=company, code="4000", name_ar="الإيرادات", nature="revenue",
        )

        prior_year = JournalEntry.objects.create(
            company=company, branch=branch, fiscal_year=year_2026,
            entry_number="2026-01", entry_date=date(2026, 1, 2),
            description="رصيد متراكم قبل الإقفال",
        )
        JournalLine.objects.create(entry=prior_year, account=cash, debit=Decimal("250"))
        JournalLine.objects.create(entry=prior_year, account=capital, credit=Decimal("250"))
        post_journal(prior_year.pk)

        opening_2027 = JournalEntry.objects.create(
            company=company, branch=branch, fiscal_year=year_2027,
            entry_number="2027-OPEN", entry_date=date(2027, 1, 1),
            description="الأرصدة المرحلة من السنة السابقة",
            source_type=JournalEntry.Source.OPENING,
        )
        JournalLine.objects.create(entry=opening_2027, account=cash, debit=Decimal("250"))
        JournalLine.objects.create(entry=opening_2027, account=capital, credit=Decimal("250"))
        post_journal(opening_2027.pk)

        current_year = JournalEntry.objects.create(
            company=company, branch=branch, fiscal_year=year_2027,
            entry_number="2027-02", entry_date=date(2027, 1, 2),
            description="حركة السنة الجديدة",
        )
        JournalLine.objects.create(entry=current_year, account=cash, debit=Decimal("30"))
        JournalLine.objects.create(entry=current_year, account=revenue, credit=Decimal("30"))
        post_journal(current_year.pk)

        user = User.objects.create_user(username="opening_reader", password="test-password")
        user.user_permissions.add(Permission.objects.get(codename="view_journalentry"))
        CompanyMembership.objects.create(user=user, company=company, role="viewer", all_branches=True)
        client = APIClient()
        client.force_authenticate(user=user)

        balance = client.get(
            f"/api/accounting/journal-entries/balance-sheet/?company={company.pk}&as_of=2027-01-02"
        )
        ledger = client.get(
            f"/api/accounting/journal-entries/general-ledger/?company={company.pk}"
            f"&account={cash.pk}&date_from=2027-01-01&date_to=2027-01-02"
        )
        equity = client.get(
            f"/api/accounting/journal-entries/changes-in-equity/?company={company.pk}"
            "&date_from=2027-01-01&date_to=2027-01-02"
        )
        trial_balance = client.get(
            f"/api/accounting/journal-entries/trial-balance/?company={company.pk}"
            "&date_from=2027-01-01&date_to=2027-01-02"
        )
        self.assertEqual(balance.status_code, 200, balance.data)
        self.assertEqual(ledger.status_code, 200, ledger.data)
        self.assertEqual(equity.status_code, 200, equity.data)
        self.assertEqual(trial_balance.status_code, 200, trial_balance.data)
        self.assertEqual(balance.data["total_assets"], Decimal("280.0000"))
        self.assertEqual(balance.data["total_equity"], Decimal("280.0000"))
        self.assertEqual(balance.data["difference"], Decimal("0.0000"))
        self.assertEqual(ledger.data["opening_balance"], Decimal("250.0000"))
        self.assertEqual(ledger.data["closing_balance"], Decimal("280.0000"))
        self.assertEqual(equity.data["opening_equity"], Decimal("250.0000"))
        self.assertEqual(equity.data["period_net_profit"], Decimal("30.0000"))
        self.assertEqual(equity.data["closing_equity"], Decimal("280.0000"))
        cash_trial_balance = next(
            row for row in trial_balance.data["accounts"] if row["account_id"] == cash.pk
        )
        self.assertEqual(cash_trial_balance["opening_balance"], Decimal("250.0000"))
        self.assertEqual(cash_trial_balance["debit_total"], Decimal("30.0000"))
        self.assertEqual(cash_trial_balance["closing_balance"], Decimal("280.0000"))
        self.assertEqual(trial_balance.data["closing_debit_credit_difference"], Decimal("0.0000"))


class BranchSpecificOpeningBalanceTests(TestCase):
    def test_reports_keep_carry_forward_for_branches_without_current_year_opening(self):
        currency = Currency.objects.create(code="EGP", name_ar="جنيه مصري")
        company = Company.objects.create(code="BR-OPEN", name_ar="شركة افتتاحيات الفروع", country="EG",
            activity="commercial", base_currency=currency)
        main = Branch.objects.create(company=company, code="MAIN", name_ar="الرئيسي", is_head_office=True)
        east = Branch.objects.create(company=company, code="EAST", name_ar="الشرقي")
        year_2026 = FiscalYear.objects.create(company=company, name="2026",
            start_date=date(2026, 1, 1), end_date=date(2026, 12, 31))
        year_2027 = FiscalYear.objects.create(company=company, name="2027",
            start_date=date(2027, 1, 1), end_date=date(2027, 12, 31))
        cash = ChartOfAccount.objects.create(company=company, code="1000", name_ar="النقدية", nature="asset")
        capital = ChartOfAccount.objects.create(company=company, code="3000", name_ar="رأس المال", nature="equity")
        revenue = ChartOfAccount.objects.create(company=company, code="4000", name_ar="الإيرادات", nature="revenue")

        for branch, number, amount in ((main, "MAIN-2026", "100"), (east, "EAST-2026", "200")):
            prior = JournalEntry.objects.create(company=company, branch=branch, fiscal_year=year_2026,
                entry_number=number, entry_date=date(2026, 12, 30), description="رصيد مرحل")
            JournalLine.objects.create(entry=prior, account=cash, debit=Decimal(amount))
            JournalLine.objects.create(entry=prior, account=capital, credit=Decimal(amount))
            post_journal(prior.pk)

        opening = JournalEntry.objects.create(company=company, branch=main, fiscal_year=year_2027,
            entry_number="MAIN-OPEN-2027", entry_date=date(2027, 1, 1), description="افتتاحي الفرع الرئيسي",
            source_type=JournalEntry.Source.OPENING)
        JournalLine.objects.create(entry=opening, account=cash, debit=Decimal("100"))
        JournalLine.objects.create(entry=opening, account=capital, credit=Decimal("100"))
        post_journal(opening.pk)

        for branch, number, amount in ((main, "MAIN-2027", "10"), (east, "EAST-2027", "20")):
            movement = JournalEntry.objects.create(company=company, branch=branch, fiscal_year=year_2027,
                entry_number=number, entry_date=date(2027, 1, 2), description="حركة الفترة")
            JournalLine.objects.create(entry=movement, account=cash, debit=Decimal(amount))
            JournalLine.objects.create(entry=movement, account=revenue, credit=Decimal(amount))
            post_journal(movement.pk)

        user = User.objects.create_user(username="branch_opening_reader", password="test-password")
        user.user_permissions.add(Permission.objects.get(codename="view_journalentry"))
        CompanyMembership.objects.create(user=user, company=company, role="viewer", all_branches=True)
        client = APIClient()
        client.force_authenticate(user=user)

        trial = client.get(f"/api/accounting/journal-entries/trial-balance/?company={company.pk}"
            "&date_from=2027-01-01&date_to=2027-01-02")
        ledger = client.get(f"/api/accounting/journal-entries/general-ledger/?company={company.pk}"
            f"&account={cash.pk}&date_from=2027-01-01&date_to=2027-01-02")
        equity = client.get(f"/api/accounting/journal-entries/changes-in-equity/?company={company.pk}"
            "&date_from=2027-01-01&date_to=2027-01-02")

        self.assertEqual(trial.status_code, 200, trial.data)
        self.assertEqual(ledger.status_code, 200, ledger.data)
        self.assertEqual(equity.status_code, 200, equity.data)
        cash_row = next(row for row in trial.data["accounts"] if row["account_id"] == cash.pk)
        self.assertEqual(cash_row["opening_balance"], Decimal("300.0000"))
        self.assertEqual(cash_row["debit_total"], Decimal("30.0000"))
        self.assertEqual(cash_row["closing_balance"], Decimal("330.0000"))
        self.assertEqual(trial.data["closing_debit_credit_difference"], Decimal("0.0000"))
        self.assertEqual(ledger.data["opening_balance"], Decimal("300.0000"))
        self.assertEqual(ledger.data["closing_balance"], Decimal("330.0000"))
        self.assertEqual(equity.data["opening_equity"], Decimal("300.0000"))
        self.assertEqual(equity.data["period_net_profit"], Decimal("30.0000"))
        self.assertEqual(equity.data["closing_equity"], Decimal("330.0000"))


class TrialBalanceClosingFilterTests(TestCase):
    def test_closing_entries_can_be_excluded_from_trial_balance(self):
        currency = Currency.objects.create(code="EGP", name_ar="جنيه مصري")
        company = Company.objects.create(code="TB-CLOSE", name_ar="شركة ميزان", country="EG",
            activity="commercial", base_currency=currency)
        branch = Branch.objects.create(company=company, code="MAIN", name_ar="الرئيسي")
        year = FiscalYear.objects.create(company=company, name="2026",
            start_date=date(2026, 1, 1), end_date=date(2026, 12, 31))
        cash = ChartOfAccount.objects.create(company=company, code="1000", name_ar="النقدية", nature="asset")
        capital = ChartOfAccount.objects.create(company=company, code="3000", name_ar="حقوق الملكية", nature="equity")
        closing = JournalEntry.objects.create(company=company, branch=branch, fiscal_year=year,
            entry_number="CLOSE-1", entry_date=date(2026, 12, 31), description="قيد إقفال",
            source_type=JournalEntry.Source.CLOSING)
        JournalLine.objects.create(entry=closing, account=cash, debit=Decimal("100"))
        JournalLine.objects.create(entry=closing, account=capital, credit=Decimal("100"))
        post_journal(closing.pk)
        user = User.objects.create_user(username="tb_closing_reader", password="test-password")
        user.user_permissions.add(Permission.objects.get(codename="view_journalentry"))
        CompanyMembership.objects.create(user=user, company=company, role="viewer", all_branches=True)
        client = APIClient()
        client.force_authenticate(user=user)
        excluded = client.get(f"/api/accounting/journal-entries/trial-balance/?company={company.pk}")
        included = client.get(f"/api/accounting/journal-entries/trial-balance/?company={company.pk}&exclude_closing=false")
        ledger_excluded = client.get(
            f"/api/accounting/journal-entries/general-ledger/?company={company.pk}&account={cash.pk}&date_from=2026-01-01&date_to=2026-12-31"
        )
        ledger_included = client.get(
            f"/api/accounting/journal-entries/general-ledger/?company={company.pk}&account={cash.pk}&date_from=2026-01-01&date_to=2026-12-31&include_closing=true"
        )
        self.assertEqual(excluded.status_code, 200)
        self.assertEqual(included.status_code, 200)
        self.assertEqual(ledger_excluded.status_code, 200)
        self.assertEqual(ledger_included.status_code, 200)
        self.assertEqual(excluded.data["debit_total"], Decimal("0.0000"))
        self.assertEqual(included.data["debit_total"], Decimal("100.0000"))
        self.assertEqual(ledger_excluded.data["closing_balance"], Decimal("0.0000"))
        self.assertEqual(ledger_excluded.data["rows"], [])
        self.assertEqual(ledger_included.data["closing_balance"], Decimal("100.0000"))
        self.assertEqual(len(ledger_included.data["rows"]), 1)



class TaxCodeConfigurationApiTests(TestCase):
    def setUp(self):
        self.egp = Currency.objects.create(code="EGP", name_ar="جنيه مصري")
        self.sar = Currency.objects.create(code="SAR", name_ar="ريال سعودي")
        self.company = Company.objects.create(code="TAX-EG", name_ar="شركة مصرية", country="EG",
            activity="commercial", base_currency=self.egp)
        self.branch = Branch.objects.create(company=self.company, code="HQ", name_ar="الرئيسي")
        self.admin = User.objects.create_user(username="tax_admin", password="test-password")
        CompanyMembership.objects.create(user=self.admin, company=self.company, role="admin", all_branches=True)
        self.client = APIClient()
        self.client.force_authenticate(user=self.admin)

    def test_company_tax_code_is_created_and_filtered_by_effective_date(self):
        payload = {
            "company": self.company.pk, "country": "EG", "code": "VAT-STD",
            "name_ar": "ضريبة قياسية", "category": "standard", "rate": "14.0000",
            "effective_from": "2026-01-01", "input_tax_recoverable": True, "is_active": True,
        }
        created = self.client.post("/api/accounting/tax-codes/", payload, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        active = self.client.get(f"/api/accounting/tax-codes/?company={self.company.pk}&as_of=2026-06-01")
        before = self.client.get(f"/api/accounting/tax-codes/?company={self.company.pk}&as_of=2025-12-31")
        self.assertEqual(active.status_code, 200)
        self.assertEqual(len(active.data), 1)
        self.assertEqual(active.data[0]["rate"], "14.0000")
        self.assertEqual(before.status_code, 200)
        self.assertEqual(len(before.data), 0)

    def test_tax_code_rejects_overlapping_effective_periods(self):
        base = {
            "company": self.company.pk, "country": "EG", "code": "VAT-VERSIONED",
            "name_ar": "ضريبة قياسية", "category": "standard", "rate": "14",
            "effective_from": "2026-01-01", "input_tax_recoverable": True, "is_active": True,
        }
        first = self.client.post("/api/accounting/tax-codes/", base, format="json")
        self.assertEqual(first.status_code, 201, first.data)
        overlapping = {**base, "effective_from": "2026-06-01", "effective_to": "2026-12-31", "rate": "15"}
        second = self.client.post("/api/accounting/tax-codes/", overlapping, format="json")
        self.assertEqual(second.status_code, 400)
        self.assertIn("effective_from", second.data)

    def test_tax_code_rejects_country_mismatch_and_nonzero_exempt_rate(self):
        wrong_country = self.client.post("/api/accounting/tax-codes/", {
            "company": self.company.pk, "country": "SA", "code": "SA-STD",
            "name_ar": "ضريبة سعودية", "category": "standard", "rate": "15",
            "effective_from": "2026-01-01",
        }, format="json")
        invalid_exempt = self.client.post("/api/accounting/tax-codes/", {
            "company": self.company.pk, "country": "EG", "code": "EXEMPT",
            "name_ar": "معفى", "category": "exempt", "rate": "14",
            "effective_from": "2026-01-01",
        }, format="json")
        self.assertEqual(wrong_country.status_code, 400)
        self.assertEqual(invalid_exempt.status_code, 400)

    def test_viewer_cannot_create_tax_codes(self):
        viewer = User.objects.create_user(username="tax_viewer", password="test-password")
        CompanyMembership.objects.create(user=viewer, company=self.company, role="viewer", all_branches=True)
        self.client.force_authenticate(user=viewer)
        response = self.client.post("/api/accounting/tax-codes/", {
            "company": self.company.pk, "country": "EG", "code": "VAT-TEST",
            "name_ar": "اختبار", "category": "standard", "rate": "14",
            "effective_from": "2026-01-01",
        }, format="json")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(TaxCode.objects.count(), 0)


class JournalBaseCurrencyRoundingTests(TestCase):
    def setUp(self):
        self.currency = Currency.objects.create(code="EGP", name_ar="جنيه مصري", decimal_places=2)
        self.company = Company.objects.create(
            code="ROUND-EGP", name_ar="شركة اختبار التقريب", country="EG",
            activity="commercial", base_currency=self.currency,
        )
        self.branch = Branch.objects.create(
            company=self.company, code="HQ", name_ar="الرئيسي", is_head_office=True,
        )
        self.year = FiscalYear.objects.create(
            company=self.company, name="2026", start_date=date(2026, 1, 1),
            end_date=date(2026, 12, 31),
        )
        self.debit_account = ChartOfAccount.objects.create(
            company=self.company, code="1100", name_ar="مدين", nature="asset",
        )
        self.credit_account = ChartOfAccount.objects.create(
            company=self.company, code="4100", name_ar="دائن", nature="revenue",
        )

    def make_entry(self, number, debit, credit):
        entry = JournalEntry.objects.create(
            company=self.company, branch=self.branch, fiscal_year=self.year,
            entry_number=number, entry_date=date(2026, 11, 1), description="اختبار تقريب العملة الأساسية",
        )
        JournalLine.objects.create(entry=entry, account=self.debit_account, debit=debit, credit=0)
        JournalLine.objects.create(entry=entry, account=self.credit_account, debit=0, credit=credit)
        return entry

    def test_posting_rounds_each_line_to_egyptian_piaster(self):
        entry = self.make_entry("ROUND-2DP", Decimal("10.1250"), Decimal("10.1250"))
        posted = post_journal(entry.pk)
        lines = list(posted.lines.order_by("id"))
        self.assertEqual(lines[0].debit, Decimal("10.1300"))
        self.assertEqual(lines[1].credit, Decimal("10.1300"))
        self.assertEqual(sum((line.debit for line in lines), Decimal("0")),
                         sum((line.credit for line in lines), Decimal("0")))

    def test_posting_rejects_imbalance_created_by_minor_unit_rounding(self):
        entry = self.make_entry("ROUND-IMBALANCE", Decimal("10.1240"), Decimal("10.1250"))
        with self.assertRaises(ValidationError):
            post_journal(entry.pk)
        entry.refresh_from_db()
        self.assertEqual(entry.status, JournalEntry.Status.DRAFT)


class AccountExcelImportTests(TestCase):
    def setUp(self):
        from io import BytesIO
        from openpyxl import Workbook
        from django.core.files.uploadedfile import SimpleUploadedFile

        self.BytesIO = BytesIO
        self.Workbook = Workbook
        self.SimpleUploadedFile = SimpleUploadedFile
        self.client = APIClient()
        self.currency = Currency.objects.create(code="EGP", name_ar="جنيه مصري")
        self.company = Company.objects.create(code="XLS", name_ar="شركة الاستيراد", country="EG",
            activity="commercial", base_currency=self.currency)
        self.user = User.objects.create_user(username="excel_admin", password="test-password")
        CompanyMembership.objects.create(user=self.user, company=self.company, role="admin", all_branches=True)
        self.client.force_authenticate(user=self.user)

    def make_file(self, rows):
        workbook = self.Workbook()
        sheet = workbook.active
        sheet.append(["كود الحساب", "اسم الحساب", "كود الأب", "طبيعة الحساب", "التصنيف"])
        for row in rows:
            sheet.append(row)
        stream = self.BytesIO()
        workbook.save(stream)
        return self.SimpleUploadedFile(
            "chart.xlsx", stream.getvalue(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    def test_preview_is_read_only_and_import_creates_parent_before_child(self):
        upload = self.make_file([
            ["1000", "الأصول", None, "أصول", "مجموعة"],
            ["1100", "الأصول المتداولة", "1000", "أصول", "مجموعة"],
            ["1110", "الصندوق", "1100", "أصول", "تفصيلي"],
        ])
        url = "/api/accounting/account-import/"
        preview = self.client.post(url, {"company": self.company.pk, "action": "preview", "file": upload}, format="multipart")
        self.assertEqual(preview.status_code, 200, preview.data)
        self.assertEqual(preview.data["summary"]["ready_to_create"], 3)
        self.assertEqual(ChartOfAccount.objects.filter(company=self.company).count(), 0)

        upload = self.make_file([
            ["1000", "الأصول", None, "أصول", "مجموعة"],
            ["1100", "الأصول المتداولة", "1000", "أصول", "مجموعة"],
            ["1110", "الصندوق", "1100", "أصول", "تفصيلي"],
        ])
        imported = self.client.post(url, {"company": self.company.pk, "action": "import", "file": upload}, format="multipart")
        self.assertEqual(imported.status_code, 200, imported.data)
        self.assertEqual(imported.data["created_accounts"], ["1000", "1100", "1110"])
        cash = ChartOfAccount.objects.get(company=self.company, code="1110")
        self.assertEqual(cash.parent.code, "1100")
        self.assertTrue(cash.allow_posting)

    def test_conflicting_code_blocks_whole_import_without_changes(self):
        ChartOfAccount.objects.create(company=self.company, code="1000", name_ar="النقدية", nature="asset")
        upload = self.make_file([
            ["1000", "الأصول", None, "أصول", "مجموعة"],
            ["1100", "الأصول المتداولة", "1000", "أصول", "مجموعة"],
        ])
        response = self.client.post(
            "/api/accounting/account-import/",
            {"company": self.company.pk, "action": "import", "file": upload}, format="multipart",
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(ChartOfAccount.objects.filter(company=self.company).count(), 1)
        self.assertTrue(response.data["errors"])

    def test_duplicate_codes_and_missing_parent_are_reported(self):
        upload = self.make_file([
            ["1110", "الصندوق", "9999", "أصول", "تفصيلي"],
            ["1110", "صندوق آخر", None, "أصول", "تفصيلي"],
        ])
        response = self.client.post(
            "/api/accounting/account-import/",
            {"company": self.company.pk, "action": "preview", "file": upload}, format="multipart",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["summary"]["blocked"], 2)
        self.assertGreaterEqual(len(response.data["errors"]), 2)

    def test_non_admin_cannot_commit_excel_import(self):
        viewer = User.objects.create_user(username="excel_viewer", password="test-password")
        CompanyMembership.objects.create(user=viewer, company=self.company, role="viewer", all_branches=True)
        self.client.force_authenticate(user=viewer)
        upload = self.make_file([["1000", "الأصول", None, "أصول", "مجموعة"]])
        response = self.client.post(
            "/api/accounting/account-import/",
            {"company": self.company.pk, "action": "import", "file": upload}, format="multipart",
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(ChartOfAccount.objects.filter(company=self.company).count(), 0)

    def test_numeric_excel_codes_and_similar_names_are_safe_warnings(self):
        ChartOfAccount.objects.create(company=self.company, code="999", name_ar="الصندوق", nature="asset")
        upload = self.make_file([[1000, "الأصول", None, "أصول", "مجموعة"],
                                 [1110, "الصندوق", 1000, "أصول", "تفصيلي"]])
        preview = self.client.post(
            "/api/accounting/account-import/",
            {"company": self.company.pk, "action": "preview", "file": upload}, format="multipart",
        )
        self.assertEqual(preview.status_code, 200, preview.data)
        self.assertEqual(preview.data["summary"]["warnings"], 1)
        self.assertEqual(preview.data["summary"]["blocked"], 0)
        upload = self.make_file([[1000, "الأصول", None, "أصول", "مجموعة"],
                                 [1110, "الصندوق", 1000, "أصول", "تفصيلي"]])
        imported = self.client.post(
            "/api/accounting/account-import/",
            {"company": self.company.pk, "action": "import", "file": upload}, format="multipart",
        )
        self.assertEqual(imported.status_code, 200, imported.data)
        self.assertTrue(ChartOfAccount.objects.filter(company=self.company, code="1110").exists())

    def test_download_excel_template(self):
        response = self.client.get("/api/accounting/account-import/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("spreadsheetml", response["Content-Type"])
        self.assertIn("attachment", response["Content-Disposition"])

    def test_exact_existing_account_is_reused_without_modification(self):
        root = ChartOfAccount.objects.create(
            company=self.company, code="1000", name_ar="الأصول", nature="asset",
            is_group=True, allow_posting=False,
        )
        upload = self.make_file([
            ["1000", "الأصول", None, "أصول", "مجموعة"],
            ["1110", "الصندوق", "1000", "أصول", "تفصيلي"],
        ])
        response = self.client.post(
            "/api/accounting/account-import/",
            {"company": self.company.pk, "action": "import", "file": upload}, format="multipart",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["skipped_existing"], 1)
        self.assertEqual(response.data["created_accounts"], ["1110"])
        root.refresh_from_db()
        self.assertEqual(root.name_ar, "الأصول")
        self.assertFalse(root.allow_posting)
        self.assertEqual(ChartOfAccount.objects.filter(company=self.company, code="1000").count(), 1)

    def test_account_role_can_be_remapped_without_changing_role_identity(self):
        old = ChartOfAccount.objects.create(company=self.company, code="1110", name_ar="الصندوق القديم", nature="asset")
        new = ChartOfAccount.objects.create(company=self.company, code="1120", name_ar="الصندوق الرئيسي", nature="asset")
        role = AccountRole.objects.create(company=self.company, role="cash", account=old)
        response = self.client.patch(
            f"/api/accounting/account-roles/{role.pk}/", {"account": new.pk}, format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        role.refresh_from_db()
        self.assertEqual(role.role, "cash")
        self.assertEqual(role.company_id, self.company.pk)
        self.assertEqual(role.account_id, new.pk)

    def test_account_role_identity_and_company_cannot_be_changed(self):
        account = ChartOfAccount.objects.create(company=self.company, code="1110", name_ar="الصندوق", nature="asset")
        role = AccountRole.objects.create(company=self.company, role="cash", account=account)
        response = self.client.patch(
            f"/api/accounting/account-roles/{role.pk}/", {"role": "bank"}, format="json",
        )
        self.assertEqual(response.status_code, 400)
        role.refresh_from_db()
        self.assertEqual(role.role, "cash")

    def test_account_role_rejects_group_inactive_or_cross_company_accounts(self):
        role_account = ChartOfAccount.objects.create(company=self.company, code="1110", name_ar="الصندوق", nature="asset")
        role = AccountRole.objects.create(company=self.company, role="cash", account=role_account)
        group = ChartOfAccount.objects.create(company=self.company, code="1000", name_ar="الأصول", nature="asset",
                                              is_group=True, allow_posting=False)
        response = self.client.patch(
            f"/api/accounting/account-roles/{role.pk}/", {"account": group.pk}, format="json",
        )
        self.assertEqual(response.status_code, 400)
        role_account.is_active = False
        role_account.save()
        response = self.client.patch(
            f"/api/accounting/account-roles/{role.pk}/", {"account": role_account.pk}, format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_account_role_rejects_wrong_account_nature(self):
        cash = ChartOfAccount.objects.create(company=self.company, code="1110", name_ar="الصندوق", nature="asset")
        wrong = ChartOfAccount.objects.create(company=self.company, code="4110", name_ar="المبيعات", nature="revenue")
        role = AccountRole.objects.create(company=self.company, role="cash", account=cash)
        response = self.client.patch(
            f"/api/accounting/account-roles/{role.pk}/", {"account": wrong.pk}, format="json",
        )
        self.assertEqual(response.status_code, 400)
        role.refresh_from_db()
        self.assertEqual(role.account_id, cash.pk)


class AccountRoleRuntimeValidationTests(TestCase):
    def setUp(self):
        from accounting.services import get_account_for_role
        self.get_account_for_role = get_account_for_role
        currency = Currency.objects.create(code="EGP", name_ar="جنيه مصري")
        self.company = Company.objects.create(
            code="ROLE", name_ar="شركة اختبار الأدوار", country="EG",
            activity="commercial", base_currency=currency,
        )
        self.cash = ChartOfAccount.objects.create(
            company=self.company, code="1110", name_ar="الصندوق", nature="asset",
        )
        self.revenue = ChartOfAccount.objects.create(
            company=self.company, code="4110", name_ar="المبيعات", nature="revenue",
        )

    def test_runtime_role_lookup_rejects_wrong_account_nature(self):
        AccountRole.objects.create(company=self.company, role=AccountRole.Role.CASH, account=self.revenue)
        with self.assertRaises(ValidationError):
            self.get_account_for_role(self.company, AccountRole.Role.CASH)

    def test_runtime_role_lookup_returns_correct_nature_account(self):
        AccountRole.objects.create(company=self.company, role=AccountRole.Role.CASH, account=self.cash)
        self.assertEqual(
            self.get_account_for_role(self.company, AccountRole.Role.CASH).pk,
            self.cash.pk,
        )

    def test_runtime_role_lookup_prefers_activity_mapping_then_legacy_fallback(self):
        service_activity = CompanyActivity.objects.create(
            company=self.company, activity=Company.Activity.SERVICE,
        )
        CompanyActivity.objects.create(
            company=self.company, activity=Company.Activity.MEDICAL,
        )
        generic = AccountRole.objects.create(
            company=self.company, role=AccountRole.Role.CASH, account=self.cash,
        )
        service_cash = ChartOfAccount.objects.create(
            company=self.company, code="1111", name_ar="صندوق الخدمات", nature="asset",
        )
        AccountRole.objects.create(
            company=self.company, role=AccountRole.Role.CASH,
            account=service_cash, activity=service_activity,
        )

        self.assertEqual(
            self.get_account_for_role(self.company, AccountRole.Role.CASH, "service").pk,
            service_cash.pk,
        )
        self.assertEqual(
            self.get_account_for_role(self.company, AccountRole.Role.CASH, "medical").pk,
            generic.account_id,
        )
        self.assertEqual(
            self.get_account_for_role(self.company, AccountRole.Role.CASH).pk,
            generic.account_id,
        )

    def test_runtime_role_lookup_rejects_disabled_or_unassigned_activity(self):
        with self.assertRaises(ValidationError):
            self.get_account_for_role(self.company, AccountRole.Role.CASH, "medical")

        disabled = CompanyActivity.objects.create(
            company=self.company, activity=Company.Activity.SERVICE, is_active=False,
        )
        with self.assertRaises(ValidationError):
            self.get_account_for_role(self.company, AccountRole.Role.CASH, disabled)

    def test_expense_analysis_groups_posted_expenses_by_account_branch_and_month(self):
        currency = Currency.objects.get(code="EGP")
        company = Company.objects.create(code="EXP-AN", name_ar="شركة تحليل المصروفات",
            country="EG", activity="commercial", base_currency=currency)
        branch = Branch.objects.create(company=company, code="MAIN", name_ar="الرئيسي", is_head_office=True)
        year = FiscalYear.objects.create(company=company, name="2026",
            start_date=date(2026, 1, 1), end_date=date(2026, 12, 31))
        cash = ChartOfAccount.objects.create(company=company, code="1000", name_ar="النقدية", nature="asset")
        direct = ChartOfAccount.objects.create(company=company, code="5000", name_ar="تكلفة المبيعات", nature="expense")
        operating = ChartOfAccount.objects.create(company=company, code="6100", name_ar="مصروفات إدارية", nature="expense")
        AccountRole.objects.create(company=company, role=AccountRole.Role.COGS, account=direct)
        center = CostCenter.objects.create(company=company, code="ADM", name_ar="الإدارة")
        entry = JournalEntry.objects.create(company=company, branch=branch, fiscal_year=year,
            entry_number="EXP-AN-1", entry_date=date(2026, 2, 10), description="مصروفات فبراير")
        JournalLine.objects.create(entry=entry, account=direct, debit=Decimal("300"), credit=0)
        JournalLine.objects.create(entry=entry, account=operating, cost_center=center, debit=Decimal("120"), credit=0)
        JournalLine.objects.create(entry=entry, account=cash, debit=0, credit=Decimal("420"))
        post_journal(entry.pk)
        user = User.objects.create_user(username="expense_reader", password="test-password")
        user.user_permissions.add(Permission.objects.get(codename="view_journalentry"))
        CompanyMembership.objects.create(user=user, company=company, role="viewer", all_branches=True)
        client = APIClient()
        client.force_authenticate(user=user)
        result = client.get(
            f"/api/accounting/journal-entries/expense-analysis/?company={company.pk}"
            "&date_from=2026-02-01&date_to=2026-02-28"
        )
        self.assertEqual(result.status_code, 200, result.data)
        self.assertEqual(result.data["total"], Decimal("420.0000"))
        self.assertEqual(result.data["direct_total"], Decimal("300.0000"))
        self.assertEqual(result.data["operating_total"], Decimal("120.0000"))
        self.assertEqual(result.data["monthly_rows"][0]["month"], "2026-02")
        self.assertEqual(len(result.data["rows"]), 2)
        filtered = client.get(
            f"/api/accounting/journal-entries/expense-analysis/?company={company.pk}"
            f"&date_from=2026-02-01&date_to=2026-02-28&cost_center={center.pk}"
        )
        self.assertEqual(filtered.status_code, 200, filtered.data)
        self.assertEqual(filtered.data["total"], Decimal("120.0000"))

        reversal_user = User.objects.create_user(username="expense_reverser", password="test-password")
        CompanyMembership.objects.create(
            user=reversal_user, company=company, role="accountant", all_branches=True,
        )
        reversal_client = APIClient()
        reversal_client.force_authenticate(user=reversal_user)
        reversed_response = reversal_client.post(
            f"/api/accounting/journal-entries/{entry.pk}/reverse/",
            {"entry_date": "2026-02-20"}, format="json",
        )
        self.assertEqual(reversed_response.status_code, 201, reversed_response.data)
        after_reversal = client.get(
            f"/api/accounting/journal-entries/expense-analysis/?company={company.pk}"
            "&date_from=2026-02-01&date_to=2026-02-28"
        )
        self.assertEqual(after_reversal.status_code, 200, after_reversal.data)
        self.assertEqual(after_reversal.data["total"], Decimal("0.0000"))

    def test_vat_return_reports_ledger_reconciliation_and_unclassified_inputs(self):
        Branch.objects.create(company=self.company, code="VAT-HQ", name_ar="الرئيسي", is_head_office=True)
        user = User.objects.create_user(username="vat_reconcile_viewer", password="test-password")
        user.user_permissions.add(Permission.objects.get(codename="view_journalentry"))
        CompanyMembership.objects.create(user=user, company=self.company, role="viewer", all_branches=True)
        client = APIClient()
        client.force_authenticate(user=user)
        response = client.get(
            f"/api/accounting/journal-entries/vat-return/?company={self.company.pk}&date_from=2026-01-01&date_to=2026-01-31"
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIn("ledger_reconciliation", response.data)
        self.assertFalse(response.data["ledger_reconciliation"]["output_account_configured"])
        self.assertFalse(response.data["ledger_reconciliation"]["input_account_configured"])
        self.assertEqual(response.data["unclassified_tax"]["purchase_recoverable_amount"], "0")

    def test_country_tax_presets_are_idempotent_and_use_egypt_standard_rate(self):
        Branch.objects.create(company=self.company, code="VAT-ADMIN", name_ar="الرئيسي", is_head_office=True)
        user = User.objects.create_user(username="vat_preset_admin", password="test-password")
        CompanyMembership.objects.create(user=user, company=self.company, role="admin", all_branches=True)
        client = APIClient()
        client.force_authenticate(user=user)
        url = "/api/accounting/tax-codes/initialize-country-presets/"
        first = client.post(url, {"company": self.company.pk}, format="json")
        self.assertEqual(first.status_code, 200, first.data)
        self.assertEqual(len(first.data["created"]), 4)
        standard = TaxCode.objects.get(company=self.company, code="VAT-STD")
        self.assertEqual(standard.rate, Decimal("14"))
        second = client.post(url, {"company": self.company.pk}, format="json")
        self.assertEqual(second.status_code, 200, second.data)
        self.assertEqual(len(second.data["created"]), 0)
        self.assertEqual(len(second.data["existing"]), 4)
        self.assertEqual(TaxCode.objects.filter(company=self.company).count(), 4)

    def test_country_tax_presets_do_not_create_overlapping_versions(self):
        Branch.objects.create(company=self.company, code="VAT-OVERLAP", name_ar="الرئيسي", is_head_office=True)
        TaxCode.objects.create(
            company=self.company, country="EG", code="VAT-STD", name_ar="نسبة قياسية حالية",
            category=TaxCode.Category.STANDARD, rate=Decimal("14"),
            effective_from=date(2026, 1, 1), input_tax_recoverable=True,
        )
        user = User.objects.create_user(username="vat_overlap_admin", password="test-password")
        CompanyMembership.objects.create(user=user, company=self.company, role="admin", all_branches=True)
        client = APIClient()
        client.force_authenticate(user=user)
        response = client.post(
            "/api/accounting/tax-codes/initialize-country-presets/",
            {"company": self.company.pk}, format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(TaxCode.objects.filter(company=self.company, code="VAT-STD").count(), 1)
        self.assertEqual(len(response.data["existing"]), 1)



from rest_framework.test import APITestCase

from inventory.models import StockMovement
from django.utils import timezone


class SubledgerReconciliationApiTests(APITestCase):
    def setUp(self):
        currency = Currency.objects.create(code="EGP", name_ar="جنيه مصري")
        self.company = Company.objects.create(
            code="RECON", name_ar="شركة اختبار المطابقة", country="EG",
            activity="commercial", base_currency=currency,
        )
        self.branch = Branch.objects.create(
            company=self.company, code="MAIN", name_ar="الرئيسي", is_head_office=True,
        )
        self.year = FiscalYear.objects.create(
            company=self.company, name="2026",
            start_date=date(2026, 1, 1), end_date=date(2026, 12, 31),
        )
        self.user = User.objects.create_user(username="recon-user", password="test-password")
        CompanyMembership.objects.create(
            user=self.user, company=self.company, role="accountant", all_branches=True,
        )
        self.client.force_authenticate(self.user)
        self.receivables = ChartOfAccount.objects.create(
            company=self.company, code="1100", name_ar="العملاء", nature="asset",
        )
        self.cash = ChartOfAccount.objects.create(
            company=self.company, code="1000", name_ar="الصندوق", nature="asset",
        )
        AccountRole.objects.create(
            company=self.company, role=AccountRole.Role.ACCOUNTS_RECEIVABLE, account=self.receivables,
        )

    def test_manual_control_account_movement_is_reported_as_a_difference(self):
        entry = JournalEntry.objects.create(
            company=self.company, branch=self.branch, fiscal_year=self.year,
            entry_number="MANUAL-AR-001", entry_date=date(2026, 3, 10),
            description="حركة يدوية على حساب العملاء",
        )
        JournalLine.objects.create(entry=entry, account=self.receivables, debit=Decimal("125.2500"))
        JournalLine.objects.create(entry=entry, account=self.cash, credit=Decimal("125.2500"))
        post_journal(entry.pk)

        result = self.client.get(
            f"/api/accounting/subledger-reconciliation/?company={self.company.pk}&as_of=2026-03-31"
        )
        self.assertEqual(result.status_code, 200, result.data)
        row = next(item for item in result.data["rows"] if item["key"] == "customers")
        self.assertEqual(row["status"], "difference")
        self.assertEqual(Decimal(row["subledger_balance"]), Decimal("0"))
        self.assertEqual(Decimal(row["gl_balance"]), Decimal("125.2500"))
        self.assertEqual(Decimal(row["difference"]), Decimal("125.2500"))
        self.assertEqual(row["unmatched_entries"][0]["entry_number"], "MANUAL-AR-001")

    def test_customer_receipt_batch_is_included_once_in_customer_reconciliation(self):
        from customers.models import Customer
        from sales.models import CustomerReceiptBatch

        customer = Customer.objects.create(
            company=self.company, code="RECON-CUST-001", name_ar="عميل دفعة مجمعة",
        )
        entry = JournalEntry.objects.create(
            company=self.company, branch=self.branch, fiscal_year=self.year,
            entry_number="BATCH-RECON-001", entry_date=date(2026, 3, 10),
            description="تحصيل دفعة مقدمة للعميل",
        )
        JournalLine.objects.create(
            entry=entry, account=self.cash, debit=Decimal("125.0000"),
        )
        JournalLine.objects.create(
            entry=entry, account=self.receivables, credit=Decimal("125.0000"),
        )
        post_journal(entry.pk)
        CustomerReceiptBatch.objects.create(
            company=self.company, customer=customer, branch=self.branch,
            receipt_number="BATCH-RECON-001", receipt_date=date(2026, 3, 10),
            method="cash", amount=Decimal("125.0000"),
            status=CustomerReceiptBatch.Status.POSTED, journal_entry=entry,
            created_by=self.user,
        )

        result = self.client.get(
            f"/api/accounting/subledger-reconciliation/?company={self.company.pk}&as_of=2026-03-31"
        )
        self.assertEqual(result.status_code, 200, result.data)
        customers = next(row for row in result.data["rows"] if row["key"] == "customers")
        self.assertEqual(customers["status"], "matched", customers)
        self.assertEqual(Decimal(customers["subledger_balance"]), Decimal("-125.0000"))
        self.assertEqual(Decimal(customers["gl_balance"]), Decimal("-125.0000"))
        self.assertEqual(Decimal(customers["difference"]), Decimal("0"))
        self.assertEqual(customers["source_journal_count"], 1)
        self.assertEqual(customers["unmatched_entry_count"], 0)

    def test_customer_refund_is_included_once_in_customer_subledger_reconciliation(self):
        from customers.models import Customer
        from sales.models import CustomerReceiptBatch, CustomerRefund

        customer = Customer.objects.create(
            company=self.company, code="RECON-REFUND-CUST", name_ar="عميل رد مبلغ",
        )
        batch_entry = JournalEntry.objects.create(
            company=self.company, branch=self.branch, fiscal_year=self.year,
            entry_number="REFUND-BATCH-RECON", entry_date=date(2026, 3, 10),
            description="دفعة مقدمة للعميل",
        )
        JournalLine.objects.create(entry=batch_entry, account=self.cash, debit=Decimal("125"))
        JournalLine.objects.create(entry=batch_entry, account=self.receivables, credit=Decimal("125"))
        post_journal(batch_entry.pk)
        batch = CustomerReceiptBatch.objects.create(
            company=self.company, customer=customer, branch=self.branch,
            receipt_number="REFUND-BATCH-RECON", receipt_date=date(2026, 3, 10),
            method="cash", amount=Decimal("125"),
            status=CustomerReceiptBatch.Status.POSTED, journal_entry=batch_entry,
            created_by=self.user,
        )

        refund_entry = JournalEntry.objects.create(
            company=self.company, branch=self.branch, fiscal_year=self.year,
            entry_number="REFUND-RECON-001", entry_date=date(2026, 3, 12),
            description="رد جزء من دفعة العميل",
        )
        JournalLine.objects.create(entry=refund_entry, account=self.receivables, debit=Decimal("25"))
        JournalLine.objects.create(entry=refund_entry, account=self.cash, credit=Decimal("25"))
        post_journal(refund_entry.pk)
        treasury_account = TreasuryAccount.objects.create(
            company=self.company, branch=self.branch, account=self.cash,
            kind=TreasuryAccount.Kind.CASH, code="REFUND-RECON-CASH",
            name_ar="خزينة رد المبالغ",
        )
        treasury_transaction = TreasuryTransaction.objects.create(
            company=self.company, branch=self.branch, fiscal_year=self.year,
            source_account=treasury_account, counter_account=self.receivables,
            kind=TreasuryTransaction.Kind.PAYMENT,
            status=TreasuryTransaction.Status.POSTED,
            document_number="REFUND-RECON-001", transaction_date=date(2026, 3, 12),
            amount=Decimal("25"), description="رد جزء من دفعة العميل",
            journal_entry=refund_entry, created_by=self.user,
        )
        CustomerRefund.objects.create(
            company=self.company, customer=customer, branch=self.branch,
            source_receipt_batch=batch, treasury_account=treasury_account,
            treasury_transaction=treasury_transaction,
            refund_number="REFUND-RECON-001", refund_date=date(2026, 3, 12),
            amount=Decimal("25"), status=CustomerRefund.Status.POSTED,
            created_by=self.user,
        )

        result = self.client.get(
            f"/api/accounting/subledger-reconciliation/?company={self.company.pk}&as_of=2026-03-31"
        )
        self.assertEqual(result.status_code, 200, result.data)
        customers = next(row for row in result.data["rows"] if row["key"] == "customers")
        self.assertEqual(customers["status"], "matched", customers)
        self.assertEqual(Decimal(customers["subledger_balance"]), Decimal("-100.0000"))
        self.assertEqual(Decimal(customers["gl_balance"]), Decimal("-100.0000"))
        self.assertEqual(Decimal(customers["difference"]), Decimal("0"))
        self.assertEqual(customers["source_journal_count"], 2)
        self.assertEqual(customers["unmatched_entry_count"], 0)

    def test_customer_refund_missing_treasury_or_reversal_journal_is_reported(self):
        from customers.models import Customer
        from sales.models import CustomerReceiptBatch, CustomerRefund
        from treasury.models import TreasuryAccount

        customer = Customer.objects.create(
            company=self.company, code="RECON-REFUND-MISSING",
            name_ar="عميل رد غير مربوط",
        )
        batch_entry = JournalEntry.objects.create(
            company=self.company, branch=self.branch, fiscal_year=self.year,
            entry_number="REFUND-MISSING-BATCH", entry_date=date(2026, 3, 10),
            description="دفعة مقدمة للعميل",
        )
        JournalLine.objects.create(
            entry=batch_entry, account=self.cash, debit=Decimal("100"),
        )
        JournalLine.objects.create(
            entry=batch_entry, account=self.receivables, credit=Decimal("100"),
        )
        post_journal(batch_entry.pk)
        batch = CustomerReceiptBatch.objects.create(
            company=self.company, customer=customer, branch=self.branch,
            receipt_number="REFUND-MISSING-BATCH", receipt_date=date(2026, 3, 10),
            method="cash", amount=Decimal("100"),
            status=CustomerReceiptBatch.Status.POSTED,
            journal_entry=batch_entry, created_by=self.user,
        )
        treasury_account = TreasuryAccount.objects.create(
            company=self.company, branch=self.branch, account=self.cash,
            kind=TreasuryAccount.Kind.CASH, code="REFUND-MISSING-CASH",
            name_ar="خزينة اختبار الرد غير المربوط",
        )
        CustomerRefund.objects.create(
            company=self.company, customer=customer, branch=self.branch,
            source_receipt_batch=batch, treasury_account=treasury_account,
            refund_number="REFUND-MISSING-001", refund_date=date(2026, 3, 12),
            amount=Decimal("10"), status=CustomerRefund.Status.POSTED,
            created_by=self.user,
        )
        CustomerRefund.objects.create(
            company=self.company, customer=customer, branch=self.branch,
            source_receipt_batch=batch, treasury_account=treasury_account,
            refund_number="REFUND-MISSING-002", refund_date=date(2026, 3, 13),
            amount=Decimal("10"), status=CustomerRefund.Status.REVERSED,
            reversal_date=date(2026, 3, 14), created_by=self.user,
        )

        result = self.client.get(
            f"/api/accounting/subledger-reconciliation/?company={self.company.pk}&as_of=2026-03-31"
        )
        self.assertEqual(result.status_code, 200, result.data)
        self.assertEqual(result.data["posted_documents_missing_journal"]["sales"], 3)
        self.assertEqual(result.data["posted_documents_missing_journal_count"], 3)
        self.assertFalse(result.data["is_reconciled"])

    def test_missing_control_account_is_not_silently_reported_as_zero(self):
        result = self.client.get(
            f"/api/accounting/subledger-reconciliation/?company={self.company.pk}&as_of=2026-03-31"
        )
        self.assertEqual(result.status_code, 200, result.data)
        suppliers = next(item for item in result.data["rows"] if item["key"] == "suppliers")
        self.assertEqual(suppliers["status"], "not_configured")
        self.assertIsNone(suppliers["gl_balance"])


    def test_draft_inventory_movement_blocks_a_clean_reconciliation_status(self):
        inventory = ChartOfAccount.objects.create(
            company=self.company, code="1200", name_ar="المخزون", nature="asset",
        )
        AccountRole.objects.create(
            company=self.company, role=AccountRole.Role.INVENTORY, account=inventory,
        )
        StockMovement.objects.create(
            company=self.company, movement_number="DRAFT-STOCK-001",
            movement_type=StockMovement.MovementType.ADJUSTMENT,
            movement_date=timezone.localdate(),
        )
        result = self.client.get(
            f"/api/accounting/subledger-reconciliation/?company={self.company.pk}"
        )
        self.assertEqual(result.status_code, 200, result.data)
        stock = next(item for item in result.data["rows"] if item["key"] == "inventory")
        self.assertEqual(stock["status"], "pending_documents")
        self.assertEqual(stock["pending_documents_count"], 1)
        self.assertFalse(result.data["is_reconciled"])

    def test_historical_inventory_value_is_rebuilt_when_posted_movement_ledger_matches_today(self):
        inventory = ChartOfAccount.objects.create(
            company=self.company, code="1200", name_ar="المخزون", nature="asset",
        )
        AccountRole.objects.create(
            company=self.company, role=AccountRole.Role.INVENTORY, account=inventory,
        )
        result = self.client.get(
            f"/api/accounting/subledger-reconciliation/?company={self.company.pk}&as_of=2026-03-31"
        )
        self.assertEqual(result.status_code, 200, result.data)
        stock = next(item for item in result.data["rows"] if item["key"] == "inventory")
        self.assertEqual(stock["status"], "matched")
        self.assertEqual(Decimal(stock["difference"]), Decimal("0"))
        self.assertFalse(result.data["is_reconciled"])

    def test_zero_difference_customer_document_balance_matches_control_account(self):
        result = self.client.get(
            f"/api/accounting/subledger-reconciliation/?company={self.company.pk}&as_of=2026-03-31"
        )
        self.assertEqual(result.status_code, 200, result.data)
        customers = next(item for item in result.data["rows"] if item["key"] == "customers")
        self.assertEqual(customers["status"], "matched")
        self.assertEqual(Decimal(customers["subledger_balance"]), Decimal("0"))
        self.assertEqual(Decimal(customers["difference"]), Decimal("0"))
        self.assertEqual(result.data["partial_reconciliation_count"], 0)
        self.assertFalse(result.data["is_reconciled"])

    def test_payroll_partial_limitation_remains_visible_with_pending_cycle(self):
        from hr.models import PayrollCycle

        payroll_account = ChartOfAccount.objects.create(
            company=self.company, code="2101", name_ar="مرتبات مستحقة", nature="liability",
        )
        AccountRole.objects.create(
            company=self.company, role=AccountRole.Role.PAYROLL_PAYABLE, account=payroll_account,
        )
        PayrollCycle.objects.create(
            company=self.company, period_start=date(2026, 3, 1),
            period_end=date(2026, 3, 31), payment_date=date(2026, 4, 1),
            status=PayrollCycle.Status.DRAFT,
        )

        result = self.client.get(
            f"/api/accounting/subledger-reconciliation/?company={self.company.pk}&as_of=2026-03-31"
        )
        self.assertEqual(result.status_code, 200, result.data)
        payroll = next(item for item in result.data["rows"] if item["key"] == "payroll")
        self.assertEqual(payroll["status"], "pending_documents")
        self.assertEqual(payroll["pending_documents_count"], 1)
        self.assertEqual(result.data["partial_reconciliation_count"], 0)
        self.assertFalse(result.data["is_reconciled"])

    def test_statutory_payroll_liabilities_reconcile_without_changing_posted_journal(self):
        from hr.models import (
            Employee, PayrollComponent, PayrollCycle, PayrollPosting,
            PayrollResult, PayrollResultLine,
        )

        accounts = {
            "payroll": ChartOfAccount.objects.create(
                company=self.company, code="2100", name_ar="مرتبات مستحقة", nature="liability",
            ),
            "employee_insurance": ChartOfAccount.objects.create(
                company=self.company, code="2110", name_ar="تأمينات الموظف", nature="liability",
            ),
            "tax": ChartOfAccount.objects.create(
                company=self.company, code="2120", name_ar="ضريبة المرتبات", nature="liability",
            ),
            "employer_insurance": ChartOfAccount.objects.create(
                company=self.company, code="2130", name_ar="تأمينات صاحب العمل", nature="liability",
            ),
            "payroll_expense": ChartOfAccount.objects.create(
                company=self.company, code="5100", name_ar="مصروف المرتبات", nature="expense",
            ),
            "employer_expense": ChartOfAccount.objects.create(
                company=self.company, code="5110", name_ar="مصروف تأمينات صاحب العمل", nature="expense",
            ),
        }
        for role, key in (
            (AccountRole.Role.PAYROLL_PAYABLE, "payroll"),
            (AccountRole.Role.EMPLOYEE_INSURANCE_PAYABLE, "employee_insurance"),
            (AccountRole.Role.PAYROLL_TAX_PAYABLE, "tax"),
            (AccountRole.Role.EMPLOYER_INSURANCE_PAYABLE, "employer_insurance"),
        ):
            AccountRole.objects.create(company=self.company, role=role, account=accounts[key])

        employee = Employee.objects.create(
            company=self.company, branch=self.branch, code="EMP-STAT-001",
            full_name_ar="موظف التزامات", hire_date=date(2025, 1, 1),
            currency=self.company.base_currency,
        )
        cycle = PayrollCycle.objects.create(
            company=self.company, period_start=date(2026, 3, 1),
            period_end=date(2026, 3, 31), payment_date=date(2026, 4, 1),
            status=PayrollCycle.Status.POSTED,
        )
        result = PayrollResult.objects.create(
            cycle=cycle, employee=employee, basic_salary_snapshot=Decimal("10000"),
            earnings_total=Decimal("10000"), employee_deductions_total=Decimal("1500"),
            employer_share_total=Decimal("1500"), employer_share_display_addition=Decimal("1500"),
            employer_share_display_offset=Decimal("1500"), net_pay=Decimal("8500"),
            company_cost=Decimal("11500"),
        )
        employee_insurance = PayrollComponent.objects.create(
            company=self.company, code="EMP-INS", name_ar="تأمينات الموظف",
            kind=PayrollComponent.Kind.DEDUCTION,
            method=PayrollComponent.Method.EMPLOYEE_INSURANCE,
            payable_account=accounts["employee_insurance"],
        )
        tax = PayrollComponent.objects.create(
            company=self.company, code="PAY-TAX", name_ar="ضريبة المرتبات",
            kind=PayrollComponent.Kind.DEDUCTION,
            method=PayrollComponent.Method.PROGRESSIVE_TAX,
            payable_account=accounts["tax"],
        )
        employer_insurance = PayrollComponent.objects.create(
            company=self.company, code="ER-INS", name_ar="تأمينات صاحب العمل",
            kind=PayrollComponent.Kind.EMPLOYER_SHARE,
            method=PayrollComponent.Method.EMPLOYER_INSURANCE,
            expense_account=accounts["employer_expense"],
            payable_account=accounts["employer_insurance"],
        )
        PayrollResultLine.objects.create(
            result=result, component=employee_insurance, code_snapshot="EMP-INS",
            name_snapshot="تأمينات الموظف", kind_snapshot="deduction", amount=Decimal("1000"),
        )
        PayrollResultLine.objects.create(
            result=result, component=tax, code_snapshot="PAY-TAX",
            name_snapshot="ضريبة المرتبات", kind_snapshot="deduction", amount=Decimal("500"),
        )
        PayrollResultLine.objects.create(
            result=result, component=employer_insurance, code_snapshot="ER-INS",
            name_snapshot="تأمينات صاحب العمل", kind_snapshot="employer_share",
            amount=Decimal("1500"), display_only=True,
        )

        journal = JournalEntry.objects.create(
            company=self.company, branch=self.branch, fiscal_year=self.year,
            entry_number="PAY-STAT-001", entry_date=date(2026, 3, 31),
            description="قيد مرتبات واستمارات الالتزامات",
        )
        JournalLine.objects.create(entry=journal, account=accounts["payroll_expense"], debit=Decimal("10000"))
        JournalLine.objects.create(entry=journal, account=accounts["employer_expense"], debit=Decimal("1500"))
        JournalLine.objects.create(entry=journal, account=accounts["payroll"], credit=Decimal("8500"))
        JournalLine.objects.create(entry=journal, account=accounts["employee_insurance"], credit=Decimal("1000"))
        JournalLine.objects.create(entry=journal, account=accounts["tax"], credit=Decimal("500"))
        JournalLine.objects.create(entry=journal, account=accounts["employer_insurance"], credit=Decimal("1500"))
        post_journal(journal.pk)
        journal.refresh_from_db()
        PayrollPosting.objects.create(cycle=cycle, branch=self.branch, journal_entry=journal)

        original_lines = list(journal.lines.order_by("account_id").values_list("account_id", "debit", "credit"))
        original_status = journal.status
        original_journal_count = JournalEntry.objects.filter(company=self.company).count()
        original_payroll_line_count = PayrollResultLine.objects.filter(result__cycle=cycle).count()
        original_cycle_status = cycle.status
        response = self.client.get(
            f"/api/accounting/subledger-reconciliation/?company={self.company.pk}&as_of=2026-03-31"
        )
        self.assertEqual(response.status_code, 200, response.data)
        rows = {item["key"]: item for item in response.data["rows"]}
        for key, amount in (
            ("payroll_employee_insurance", Decimal("1000")),
            ("payroll_payroll_tax", Decimal("500")),
            ("payroll_employer_insurance", Decimal("1500")),
        ):
            self.assertEqual(rows[key]["status"], "partial_reconciliation")
            self.assertEqual(Decimal(rows[key]["subledger_balance"]), amount)
            self.assertEqual(Decimal(rows[key]["gl_balance"]), amount)
        payroll = rows["payroll"]
        self.assertEqual(payroll["status"], "matched")
        self.assertEqual(Decimal(payroll["subledger_balance"]), Decimal("8500"))
        journal.refresh_from_db()
        self.assertEqual(journal.status, original_status)
        self.assertEqual(
            list(journal.lines.order_by("account_id").values_list("account_id", "debit", "credit")),
            original_lines,
        )
        self.assertEqual(JournalEntry.objects.filter(company=self.company).count(), original_journal_count)
        self.assertEqual(PayrollResultLine.objects.filter(result__cycle=cycle).count(), original_payroll_line_count)
        cycle.refresh_from_db()
        self.assertEqual(cycle.status, original_cycle_status)

    def test_unpaid_payroll_net_pay_is_listed_by_employee(self):
        from hr.models import Employee, PayrollCycle, PayrollPosting, PayrollResult

        payroll_account = ChartOfAccount.objects.create(
            company=self.company, code="2100", name_ar="مرتبات مستحقة", nature="liability",
        )
        payroll_expense = ChartOfAccount.objects.create(
            company=self.company, code="5100", name_ar="مصروف المرتبات", nature="expense",
        )
        AccountRole.objects.create(
            company=self.company, role=AccountRole.Role.PAYROLL_PAYABLE, account=payroll_account,
        )
        employee = Employee.objects.create(
            company=self.company, branch=self.branch, code="EMP-001",
            full_name_ar="موظف اختبار", hire_date=date(2025, 1, 1),
            currency=self.company.base_currency,
        )
        cycle = PayrollCycle.objects.create(
            company=self.company, period_start=date(2026, 3, 1),
            period_end=date(2026, 3, 31), payment_date=date(2026, 4, 1),
            status=PayrollCycle.Status.POSTED,
        )
        PayrollResult.objects.create(
            cycle=cycle, employee=employee,
            basic_salary_snapshot=Decimal("10000"),
            earnings_total=Decimal("10000"),
            employee_deductions_total=Decimal("1000"),
            employer_share_total=Decimal("1500"),
            employer_share_display_addition=Decimal("1500"),
            employer_share_display_offset=Decimal("1500"),
            net_pay=Decimal("9000"), company_cost=Decimal("11500"),
        )
        journal = JournalEntry.objects.create(
            company=self.company, branch=self.branch, fiscal_year=self.year,
            entry_number="PAY-RECON-001", entry_date=date(2026, 3, 31),
            description="استحقاق مرتبات اختبار",
        )
        JournalLine.objects.create(entry=journal, account=payroll_expense, debit=Decimal("9000"))
        JournalLine.objects.create(entry=journal, account=payroll_account, credit=Decimal("9000"))
        post_journal(journal.pk)
        PayrollPosting.objects.create(cycle=cycle, branch=self.branch, journal_entry=journal)

        result = self.client.get(
            f"/api/accounting/subledger-reconciliation/?company={self.company.pk}&as_of=2026-03-31"
        )
        self.assertEqual(result.status_code, 200, result.data)
        payroll = next(item for item in result.data["rows"] if item["key"] == "payroll")
        self.assertEqual(payroll["status"], "matched")
        self.assertEqual(Decimal(payroll["subledger_balance"]), Decimal("9000"))
        self.assertEqual(Decimal(payroll["gl_balance"]), Decimal("9000"))
        self.assertEqual(payroll["party_count"], 1)
        self.assertEqual(payroll["party_balances"][0]["code"], "EMP-001")
        self.assertEqual(Decimal(payroll["party_balances"][0]["balance"]), Decimal("9000"))


    def test_fixed_asset_supplier_invoice_and_payment_are_in_ap_reconciliation(self):
        from assets.models import AssetPurchaseInvoice, AssetSupplierPayment, FixedAsset
        from suppliers.models import Supplier

        payable = ChartOfAccount.objects.create(
            company=self.company, code="2100", name_ar="الموردون", nature="liability",
        )
        asset_account = ChartOfAccount.objects.create(
            company=self.company, code="1500", name_ar="الأصول الثابتة", nature="asset",
        )
        accumulated = ChartOfAccount.objects.create(
            company=self.company, code="1590", name_ar="مجمع الإهلاك", nature="asset",
        )
        depreciation = ChartOfAccount.objects.create(
            company=self.company, code="6100", name_ar="مصروف الإهلاك", nature="expense",
        )
        disposal = ChartOfAccount.objects.create(
            company=self.company, code="6900", name_ar="أرباح وخسائر الأصول", nature="expense",
        )
        AccountRole.objects.create(
            company=self.company, role=AccountRole.Role.ACCOUNTS_PAYABLE, account=payable,
        )
        supplier = Supplier.objects.create(
            company=self.company, code="SUP-RECON-FA", name_ar="مورد أصول للمطابقة",
        )
        asset = FixedAsset.objects.create(
            company=self.company, branch=self.branch, code="FA-RECON-001",
            name_ar="أصل اختبار المطابقة", acquisition_date=date(2026, 3, 1),
            depreciation_start_date=date(2026, 3, 1), original_cost=Decimal("1000"),
            acquisition_net_amount=Decimal("1000"), salvage_value=Decimal("0"),
            useful_life_months=60, asset_account=asset_account,
            accumulated_depreciation_account=accumulated,
            depreciation_expense_account=depreciation,
            acquisition_counter_account=self.cash, disposal_gain_loss_account=disposal,
            disposal_loss_account=disposal,
        )

        invoice_entry = JournalEntry.objects.create(
            company=self.company, branch=self.branch, fiscal_year=self.year,
            entry_number="FA-RECON-INV-001", entry_date=date(2026, 3, 2),
            description="فاتورة شراء أصل للمطابقة", source_type=JournalEntry.Source.ASSETS,
        )
        JournalLine.objects.create(entry=invoice_entry, account=asset_account, debit=Decimal("1000"))
        JournalLine.objects.create(entry=invoice_entry, account=payable, credit=Decimal("1000"))
        post_journal(invoice_entry.pk)
        asset.acquisition_journal = invoice_entry
        asset.save(update_fields=("acquisition_journal",))

        invoice = AssetPurchaseInvoice.objects.create(
            company=self.company, branch=self.branch, asset=asset, supplier=supplier,
            fiscal_year=self.year, invoice_number="FA-RECON-INV-001",
            invoice_date=date(2026, 3, 2), net_amount=Decimal("1000"),
            tax_amount=Decimal("0"), recoverable_tax_amount=Decimal("0"),
            nonrecoverable_tax_amount=Decimal("0"), grand_total=Decimal("1000"),
            status=AssetPurchaseInvoice.Status.POSTED, journal_entry=invoice_entry,
        )

        payment_entry = JournalEntry.objects.create(
            company=self.company, branch=self.branch, fiscal_year=self.year,
            entry_number="FA-RECON-PAY-001", entry_date=date(2026, 3, 10),
            description="سداد فاتورة أصل للمطابقة", source_type=JournalEntry.Source.ASSETS,
        )
        JournalLine.objects.create(entry=payment_entry, account=payable, debit=Decimal("400"))
        JournalLine.objects.create(entry=payment_entry, account=self.cash, credit=Decimal("400"))
        post_journal(payment_entry.pk)
        AssetSupplierPayment.objects.create(
            company=self.company, branch=self.branch, invoice=invoice, supplier=supplier,
            payment_number="FA-RECON-PAY-001", payment_date=date(2026, 3, 10),
            payment_account=self.cash, amount=Decimal("400"),
            status=AssetSupplierPayment.Status.POSTED, journal_entry=payment_entry,
        )

        result = self.client.get(
            f"/api/accounting/subledger-reconciliation/?company={self.company.pk}&as_of=2026-03-31"
        )
        self.assertEqual(result.status_code, 200, result.data)
        suppliers = next(item for item in result.data["rows"] if item["key"] == "suppliers")
        self.assertEqual(suppliers["status"], "matched", suppliers)
        self.assertEqual(Decimal(suppliers["subledger_balance"]), Decimal("600"))
        self.assertEqual(Decimal(suppliers["gl_balance"]), Decimal("600"))
        self.assertEqual(Decimal(suppliers["difference"]), Decimal("0"))
        self.assertEqual(suppliers["unmatched_entry_count"], 0)
        self.assertEqual(suppliers["party_balances"][0]["party_id"], supplier.pk)
        self.assertEqual(Decimal(suppliers["party_balances"][0]["balance"]), Decimal("600"))

class HistoricalBalanceAndCashFlowRegressionTests(TestCase):
    def setUp(self):
        self.currency = Currency.objects.create(code="EGP", name_ar="جنيه مصري")
        self.company = Company.objects.create(
            code="HIST-FX", name_ar="شركة اختبار التقارير التاريخية",
            country="EG", activity="commercial", base_currency=self.currency,
        )
        self.branch = Branch.objects.create(
            company=self.company, code="HQ", name_ar="الرئيسي", is_head_office=True,
        )
        self.year = FiscalYear.objects.create(
            company=self.company, name="2026",
            start_date=date(2026, 1, 1), end_date=date(2026, 12, 31),
        )
        self.user = User.objects.create_user(
            username="historical-cashflow-reader", password="test-password",
        )
        self.user.user_permissions.add(Permission.objects.get(codename="view_journalentry"))
        CompanyMembership.objects.create(
            user=self.user, company=self.company, role="accountant", all_branches=True,
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def make_account(self, code, name, nature):
        return ChartOfAccount.objects.create(
            company=self.company, code=code, name_ar=name, nature=nature,
        )

    def test_balance_sheet_as_of_date_keeps_later_reversal_out_of_earlier_snapshot(self):
        cash = self.make_account("1000", "النقدية", ChartOfAccount.Nature.ASSET)
        capital = self.make_account("3000", "رأس المال", ChartOfAccount.Nature.EQUITY)
        original = JournalEntry.objects.create(
            company=self.company, branch=self.branch, fiscal_year=self.year,
            entry_number="HIST-OPEN-001", entry_date=date(2026, 1, 15),
            description="قيد قبل تاريخ التقرير",
        )
        JournalLine.objects.create(entry=original, account=cash, debit=Decimal("100"))
        JournalLine.objects.create(entry=original, account=capital, credit=Decimal("100"))
        post_journal(original.pk)

        reversal = JournalEntry.objects.create(
            company=self.company, branch=self.branch, fiscal_year=self.year,
            entry_number="HIST-REV-001", entry_date=date(2026, 2, 15),
            description="عكس لاحق لتاريخ التقرير", reversal_of=original,
        )
        JournalLine.objects.create(entry=reversal, account=cash, credit=Decimal("100"))
        JournalLine.objects.create(entry=reversal, account=capital, debit=Decimal("100"))
        post_journal(reversal.pk)

        january = self.client.get(
            f"/api/accounting/journal-entries/balance-sheet/?company={self.company.pk}&as_of=2026-01-31"
        )
        february = self.client.get(
            f"/api/accounting/journal-entries/balance-sheet/?company={self.company.pk}&as_of=2026-02-28"
        )

        self.assertEqual(january.status_code, 200, january.data)
        self.assertEqual(february.status_code, 200, february.data)
        self.assertEqual(january.data["total_assets"], Decimal("100.0000"))
        self.assertEqual(january.data["total_equity"], Decimal("100.0000"))
        self.assertEqual(january.data["difference"], Decimal("0.0000"))
        self.assertEqual(february.data["total_assets"], Decimal("0.0000"))
        self.assertEqual(february.data["total_equity"], Decimal("0.0000"))
        self.assertEqual(february.data["difference"], Decimal("0.0000"))

    def test_indirect_cash_flow_offsets_unrealized_fx_gain_and_loss_once(self):
        receivables = self.make_account("1100", "العملاء", ChartOfAccount.Nature.ASSET)
        payables = self.make_account("2100", "الموردون", ChartOfAccount.Nature.LIABILITY)
        fx_gain = self.make_account("4900", "أرباح فروق عملة غير محققة", ChartOfAccount.Nature.REVENUE)
        fx_loss = self.make_account("5900", "خسائر فروق عملة غير محققة", ChartOfAccount.Nature.EXPENSE)
        AccountRole.objects.create(
            company=self.company, role=AccountRole.Role.ACCOUNTS_RECEIVABLE, account=receivables,
        )
        AccountRole.objects.create(
            company=self.company, role=AccountRole.Role.ACCOUNTS_PAYABLE, account=payables,
        )
        AccountRole.objects.create(
            company=self.company, role=AccountRole.Role.UNREALIZED_FX_GAIN, account=fx_gain,
        )
        AccountRole.objects.create(
            company=self.company, role=AccountRole.Role.UNREALIZED_FX_LOSS, account=fx_loss,
        )

        gain_entry = JournalEntry.objects.create(
            company=self.company, branch=self.branch, fiscal_year=self.year,
            entry_number="CF-FX-GAIN-001", entry_date=date(2026, 3, 15),
            description="إعادة تقييم عميل - مكسب غير محقق",
        )
        JournalLine.objects.create(entry=gain_entry, account=receivables, debit=Decimal("40"))
        JournalLine.objects.create(entry=gain_entry, account=fx_gain, credit=Decimal("40"))
        post_journal(gain_entry.pk)

        loss_entry = JournalEntry.objects.create(
            company=self.company, branch=self.branch, fiscal_year=self.year,
            entry_number="CF-FX-LOSS-001", entry_date=date(2026, 3, 16),
            description="إعادة تقييم مورد - خسارة غير محققة",
        )
        JournalLine.objects.create(entry=loss_entry, account=fx_loss, debit=Decimal("25"))
        JournalLine.objects.create(entry=loss_entry, account=payables, credit=Decimal("25"))
        post_journal(loss_entry.pk)

        result = self.client.get(
            f"/api/accounting/journal-entries/indirect-cash-flow/?company={self.company.pk}"
            "&date_from=2026-03-01&date_to=2026-03-31"
        )

        self.assertEqual(result.status_code, 200, result.data)
        self.assertEqual(result.data["net_profit"], Decimal("15.0000"))
        self.assertEqual(
            result.data["non_cash_adjustments"]["unrealized_fx_gain"], Decimal("40.0000"),
        )
        self.assertEqual(
            result.data["non_cash_adjustments"]["unrealized_fx_loss"], Decimal("25.0000"),
        )
        self.assertEqual(
            result.data["non_cash_adjustments"]["unrealized_fx_net_adjustment"], Decimal("-15.0000"),
        )
        self.assertEqual(result.data["operating_cash_flow"], Decimal("0.0000"))
        self.assertEqual(result.data["ledger_cash_change"], Decimal("0.0000"))
        self.assertEqual(result.data["reconciliation_difference"], Decimal("0.0000"))



class FixedAssetIAS36ReversalTests(TestCase):
    def setUp(self):
        from assets.models import FixedAsset
        self.currency = Currency.objects.create(code="EGP", name_ar="جنيه مصري")
        self.company = Company.objects.create(
            code="FA-IAS36", name_ar="شركة اختبار الأصول", country="EG",
            activity="commercial", base_currency=self.currency,
        )
        self.branch = Branch.objects.create(
            company=self.company, code="HQ", name_ar="الرئيسي", is_head_office=True,
        )
        self.year = FiscalYear.objects.create(
            company=self.company, name="2026", start_date=date(2026, 1, 1),
            end_date=date(2026, 12, 31),
        )
        self.asset_account = ChartOfAccount.objects.create(
            company=self.company, code="1500", name_ar="تكلفة الأصول", nature="asset",
        )
        self.accumulated_depreciation = ChartOfAccount.objects.create(
            company=self.company, code="1590", name_ar="مجمع الإهلاك", nature="asset",
        )
        self.depreciation_expense = ChartOfAccount.objects.create(
            company=self.company, code="6100", name_ar="مصروف الإهلاك", nature="expense",
        )
        self.impairment_expense = ChartOfAccount.objects.create(
            company=self.company, code="6110", name_ar="خسائر انخفاض القيمة", nature="expense",
        )
        self.accumulated_impairment = ChartOfAccount.objects.create(
            company=self.company, code="1595", name_ar="مجمع انخفاض القيمة", nature="asset",
        )
        self.impairment_reversal_income = ChartOfAccount.objects.create(
            company=self.company, code="4105", name_ar="عكس خسائر انخفاض القيمة", nature="revenue",
        )
        self.bank = ChartOfAccount.objects.create(
            company=self.company, code="1000", name_ar="البنك", nature="asset",
        )
        self.disposal = ChartOfAccount.objects.create(
            company=self.company, code="6900", name_ar="أرباح استبعاد", nature="revenue",
        )
        acquisition = JournalEntry.objects.create(
            company=self.company, branch=self.branch, fiscal_year=self.year,
            entry_number="FA-IAS36-ACQ", entry_date=date(2026, 1, 1),
            description="إثبات اقتناء أصل للاختبار", source_type=JournalEntry.Source.ASSETS,
        )
        JournalLine.objects.create(entry=acquisition, account=self.asset_account, debit=Decimal("1200"))
        JournalLine.objects.create(entry=acquisition, account=self.bank, credit=Decimal("1200"))
        post_journal(acquisition.pk)
        self.asset = FixedAsset.objects.create(
            company=self.company, branch=self.branch, code="FA-IAS36-001",
            name_ar="أصل اختبار انخفاض القيمة", acquisition_date=date(2026, 1, 1),
            depreciation_start_date=date(2026, 1, 1), original_cost=Decimal("1200"),
            acquisition_net_amount=Decimal("1200"), salvage_value=Decimal("0"),
            useful_life_months=12, asset_account=self.asset_account,
            accumulated_depreciation_account=self.accumulated_depreciation,
            depreciation_expense_account=self.depreciation_expense,
            acquisition_counter_account=self.bank, disposal_gain_loss_account=self.disposal,
            disposal_loss_account=self.impairment_expense, acquisition_journal=acquisition,
        )

    def test_ias36_reversal_is_capped_and_allocated_to_original_contra_account(self):
        from django.db.models import Sum
        from assets.services import depreciate_asset, record_asset_impairment, reverse_asset_impairment
        from assets.models import AssetImpairmentReversalAllocation

        depreciate_asset(self.asset.pk, date(2026, 1, 31), self.year.pk)
        impairment = record_asset_impairment(
            self.asset.pk, date(2026, 1, 31), Decimal("800"),
            self.impairment_expense.pk, self.accumulated_impairment.pk, self.year.pk,
        )
        depreciate_asset(self.asset.pk, date(2026, 2, 28), self.year.pk)
        reversal = reverse_asset_impairment(
            self.asset.pk, date(2026, 2, 28), Decimal("1100"),
            self.impairment_reversal_income.pk, self.year.pk,
        )

        self.assertGreater(reversal.reversal_amount, Decimal("0"))
        self.assertLess(reversal.reversal_amount, impairment.impairment_loss)
        self.assertLessEqual(
            reversal.carrying_amount_before + reversal.reversal_amount,
            reversal.hypothetical_carrying_amount,
        )
        self.assertEqual(
            AssetImpairmentReversalAllocation.objects.filter(reversal=reversal).aggregate(
                total=Sum("amount")
            )["total"],
            reversal.reversal_amount,
        )
        lines = list(reversal.journal_entry.lines.all())
        self.assertEqual(
            sum((line.debit for line in lines), Decimal("0")),
            sum((line.credit for line in lines), Decimal("0")),
        )
        contra_line = next(line for line in lines if line.account_id == self.accumulated_impairment.pk)
        income_line = next(line for line in lines if line.account_id == self.impairment_reversal_income.pk)
        self.assertEqual(contra_line.debit, reversal.reversal_amount)
        self.assertEqual(income_line.credit, reversal.reversal_amount)

    def test_ias36_reversal_cannot_exceed_no_impairment_carrying_amount(self):
        from assets.services import depreciate_asset, record_asset_impairment, reverse_asset_impairment

        depreciate_asset(self.asset.pk, date(2026, 1, 31), self.year.pk)
        record_asset_impairment(
            self.asset.pk, date(2026, 1, 31), Decimal("800"),
            self.impairment_expense.pk, self.accumulated_impairment.pk, self.year.pk,
        )
        depreciate_asset(self.asset.pk, date(2026, 2, 28), self.year.pk)
        reversal = reverse_asset_impairment(
            self.asset.pk, date(2026, 2, 28), Decimal("5000"),
            self.impairment_reversal_income.pk, self.year.pk,
        )
        self.assertEqual(
            reversal.carrying_amount_before + reversal.reversal_amount,
            reversal.hypothetical_carrying_amount,
        )

