from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from accounting.models import ChartOfAccount, JournalEntry
from companies.models import Branch, Company, CompanyMembership, Currency, FiscalYear
from treasury.models import TreasuryAccount, TreasuryTransaction


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

    def _create(self, kind="receipt", **extra):
        payload = {
            "company": self.company.pk, "branch": self.branch.pk, "fiscal_year": self.year.pk,
            "source_account": self.cash.pk, "counter_account": self.revenue_gl.pk,
            "kind": kind, "document_number": "TR-DOC-001", "transaction_date": "2026-03-15",
            "amount": "250.0000", "description": "اختبار حركة خزينة",
        }
        payload.update(extra)
        return self.client.post("/api/treasury/transactions/", payload, format="json")

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
