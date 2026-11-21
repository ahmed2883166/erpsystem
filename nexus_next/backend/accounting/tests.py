from datetime import date
from decimal import Decimal
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.contrib.auth.models import Permission
from rest_framework.test import APIClient

from accounts.models import User
from companies.models import Branch, Company, CompanyMembership, Currency, FiscalYear
from .models import ChartOfAccount, JournalEntry, JournalLine
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
        main = Branch.objects.create(company=self.company, code="MAIN", name_ar="الرئيسي")
        east = Branch.objects.create(company=self.company, code="EAST", name_ar="الشرقي")
        year = FiscalYear.objects.create(
            company=self.company, name="2027",
            start_date=date(2027, 1, 1), end_date=date(2027, 12, 31),
        )
        capital = ChartOfAccount.objects.create(
            company=self.company, code="3001", name_ar="رأس المال", nature="equity"
        )
        for number, branch in (("MAIN-1", main), ("EAST-1", east)):
            entry = JournalEntry.objects.create(
                company=self.company, branch=branch, fiscal_year=year,
                entry_number=number, entry_date=date(2027, 1, 2), description="قيد فرعي",
            )
            JournalLine.objects.create(entry=entry, account=self.account, debit=Decimal("100"))
            JournalLine.objects.create(entry=entry, account=capital, credit=Decimal("100"))
            post_journal(entry.pk)
        user = User.objects.create_user(username="branch_reader", password="test-password")
        user.user_permissions.add(Permission.objects.get(codename="view_journalentry"))
        membership = CompanyMembership.objects.create(user=user, company=self.company)
        membership.branches.add(main)
        self.client.force_authenticate(user=user)
        response = self.client.get(
            f"/api/accounting/journal-entries/trial-balance/?company={self.company.pk}"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["debit_total"], Decimal("100.0000"))
        self.assertEqual(response.data["credit_total"], Decimal("100.0000"))
