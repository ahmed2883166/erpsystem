from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from companies.models import Branch, Company, CompanyMembership, Currency, FiscalYear
from accounting.models import ChartOfAccount, JournalEntry


class FiscalYearJournalSafetyTests(APITestCase):
    def setUp(self):
        self.currency = Currency.objects.create(code="EGP", name_ar="جنيه مصري")
        self.company = Company.objects.create(
            code="FY-JOURNAL", name_ar="اختبار قيود السنة", country="EG",
            activity="commercial", base_currency=self.currency,
        )
        self.branch = Branch.objects.create(
            company=self.company, code="MAIN", name_ar="الفرع الرئيسي"
        )
        self.year = FiscalYear.objects.create(
            company=self.company, name="2026",
            start_date="2026-01-01", end_date="2026-12-31",
        )
        self.admin = get_user_model().objects.create_user(
            username="fiscal-journal-admin", password="test-password"
        )
        CompanyMembership.objects.create(
            user=self.admin, company=self.company, role="admin", all_branches=True,
        )
        self.asset = ChartOfAccount.objects.create(
            company=self.company, code="1000", name_ar="نقدية", nature="asset",
            is_group=False, allow_posting=True,
        )
        self.equity = ChartOfAccount.objects.create(
            company=self.company, code="3000", name_ar="حقوق ملكية", nature="equity",
            is_group=False, allow_posting=True,
        )
        self.client.force_authenticate(self.admin)

    def payload(self, source_type, entry_date):
        return {
            "company": self.company.pk,
            "branch": self.branch.pk,
            "fiscal_year": self.year.pk,
            "entry_number": f"{source_type.upper()}-001",
            "entry_date": entry_date,
            "description": "قيد اختبار السنة المالية",
            "source_type": source_type,
            "lines": [
                {"account": self.asset.pk, "debit": "100.0000", "credit": "0"},
                {"account": self.equity.pk, "debit": "0", "credit": "100.0000"},
            ],
        }

    def test_company_admin_can_create_closing_draft_only_on_year_end(self):
        response = self.client.post(
            "/api/accounting/journals/",
            self.payload(JournalEntry.Source.CLOSING, "2026-12-31"),
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["source_type"], JournalEntry.Source.CLOSING)

    def test_opening_and_closing_journals_reject_wrong_dates(self):
        response = self.client.post(
            "/api/accounting/journals/",
            self.payload(JournalEntry.Source.CLOSING, "2026-12-30"),
            format="json",
        )
        self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(JournalEntry.objects.filter(entry_number="CLOSING-001").exists())

    def test_accountant_cannot_create_opening_or_closing_journals(self):
        accountant = get_user_model().objects.create_user(
            username="fiscal-journal-accountant", password="test-password"
        )
        CompanyMembership.objects.create(
            user=accountant, company=self.company, role="accountant", all_branches=True,
        )
        self.client.force_authenticate(accountant)
        response = self.client.post(
            "/api/accounting/journals/",
            self.payload(JournalEntry.Source.OPENING, "2026-01-01"),
            format="json",
        )
        self.assertEqual(response.status_code, 403, response.data)
        self.assertFalse(JournalEntry.objects.filter(source_type=JournalEntry.Source.OPENING).exists())

    def test_closed_fiscal_year_rejects_new_journal_drafts(self):
        self.year.status = FiscalYear.Status.CLOSED
        self.year.save(update_fields=("status",))
        response = self.client.post(
            "/api/accounting/journals/",
            self.payload(JournalEntry.Source.MANUAL, "2026-06-01"),
            format="json",
        )
        self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(JournalEntry.objects.filter(entry_number="MANUAL-001").exists())

    def test_duplicate_closing_draft_is_rejected_for_same_branch_and_year(self):
        first = self.client.post(
            "/api/accounting/journals/",
            self.payload(JournalEntry.Source.CLOSING, "2026-12-31"),
            format="json",
        )
        self.assertEqual(first.status_code, 201, first.data)
        second_payload = self.payload(JournalEntry.Source.CLOSING, "2026-12-31")
        second_payload["entry_number"] = "CLOSING-002"
        second = self.client.post(
            "/api/accounting/journals/", second_payload, format="json"
        )
        self.assertEqual(second.status_code, 400, second.data)
        self.assertEqual(
            JournalEntry.objects.filter(
                fiscal_year=self.year, branch=self.branch,
                source_type=JournalEntry.Source.CLOSING,
            ).count(),
            1,
        )

    def test_existing_draft_cannot_be_posted_after_fiscal_year_is_closed(self):
        from accounting.models import JournalLine
        entry = JournalEntry.objects.create(
            company=self.company, branch=self.branch, fiscal_year=self.year,
            entry_number="DRAFT-BEFORE-CLOSE", entry_date="2026-06-01",
            description="مسودة قبل الإقفال", source_type=JournalEntry.Source.MANUAL,
        )
        JournalLine.objects.create(
            entry=entry, account=self.asset, debit="100.0000", credit="0",
        )
        JournalLine.objects.create(
            entry=entry, account=self.equity, debit="0", credit="100.0000",
        )
        self.year.status = FiscalYear.Status.CLOSED
        self.year.save(update_fields=("status",))
        response = self.client.post(
            f"/api/accounting/journals/{entry.pk}/post/", {}, format="json"
        )
        self.assertEqual(response.status_code, 400, response.data)
        entry.refresh_from_db()
        self.assertEqual(entry.status, JournalEntry.Status.DRAFT)
