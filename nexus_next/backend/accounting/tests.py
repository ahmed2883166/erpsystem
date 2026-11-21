from datetime import date
from decimal import Decimal
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.contrib.auth.models import Permission
from rest_framework.test import APIClient

from accounts.models import User
from companies.models import Branch, Company, CompanyMembership, Currency, FiscalYear
from .models import AccountRole, ChartOfAccount, JournalEntry, JournalLine
from .services import post_journal


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

    def test_unbalanced_entry_is_rejected(self):
        entry = self.make_entry(credit="90.00")
        with self.assertRaises(ValidationError):
            post_journal(entry.pk)

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


    def test_user_cannot_read_an_unassigned_company_trial_balance(self):
        other = Company.objects.create(
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


class IndirectCashFlowApiTests(TestCase):
    def test_indirect_cash_flow_reconciles_to_cash_ledger_and_uses_opening_balances(self):
        currency = Currency.objects.create(code="EGP", name_ar="جنيه مصري")
        company = Company.objects.create(code="CF", name_ar="شركة التدفقات", country="EG",
            activity="commercial", base_currency=currency)
        branch = Branch.objects.create(company=company, code="MAIN", name_ar="الرئيسي", is_head_office=True)
        second_branch = Branch.objects.create(company=company, code="SEC", name_ar="الفرع الثاني")
        year = FiscalYear.objects.create(company=company, name="2026",
            start_date=date(2026, 1, 1), end_date=date(2026, 12, 31))
        cash = ChartOfAccount.objects.create(company=company, code="1000", name_ar="النقدية", nature="asset")
        receivable = ChartOfAccount.objects.create(company=company, code="1100", name_ar="العملاء", nature="asset")
        payable = ChartOfAccount.objects.create(company=company, code="2100", name_ar="الموردون", nature="liability")
        capital = ChartOfAccount.objects.create(company=company, code="3000", name_ar="رأس المال", nature="equity")
        revenue = ChartOfAccount.objects.create(company=company, code="4000", name_ar="المبيعات", nature="revenue")
        expense = ChartOfAccount.objects.create(company=company, code="5000", name_ar="مصروفات", nature="expense")
        AccountRole.objects.create(company=company, role=AccountRole.Role.ACCOUNTS_RECEIVABLE, account=receivable)
        AccountRole.objects.create(company=company, role=AccountRole.Role.ACCOUNTS_PAYABLE, account=payable)
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
        self.assertEqual(response.data["net_profit"], Decimal("700.0000"))
        self.assertEqual(response.data["operating_adjustments"], Decimal("-100.0000"))
        self.assertEqual(response.data["operating_cash_flow"], Decimal("600.0000"))
        self.assertEqual(response.data["ledger_cash_change"], Decimal("600.0000"))
        self.assertEqual(response.data["reconciliation_difference"], Decimal("0.0000"))


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
        included = client.get(f"/api/accounting/journal-entries/trial-balance/?company={company.pk}")
        excluded = client.get(f"/api/accounting/journal-entries/trial-balance/?company={company.pk}&exclude_closing=true")
        self.assertEqual(included.status_code, 200)
        self.assertEqual(excluded.status_code, 200)
        self.assertEqual(included.data["debit_total"], Decimal("100.0000"))
        self.assertEqual(excluded.data["debit_total"], Decimal("0.0000"))
