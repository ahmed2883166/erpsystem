from datetime import date
from decimal import Decimal
from django.core.exceptions import ValidationError
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import User
from companies.models import Branch, Company, Currency, FiscalYear
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

    def test_authenticated_user_can_read_accounts(self):
        user = User.objects.create_user(username="reader", password="test-password")
        self.client.force_authenticate(user=user)
        response = self.client.get("/api/accounting/accounts/?company=" + str(self.company.pk))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 1)
