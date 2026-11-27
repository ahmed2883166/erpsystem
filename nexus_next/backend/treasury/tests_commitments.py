from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from accounting.models import ChartOfAccount, JournalEntry, JournalLine
from accounting.services import post_journal
from companies.models import Branch, Company, CompanyMembership, Currency, ExchangeRate, FiscalYear
from treasury.models import CashCommitment, CashCommitmentOccurrence


class CashCommitmentApiTests(APITestCase):
    def setUp(self):
        self.currency = Currency.objects.create(code="EGP", name_ar="جنيه مصري")
        self.company = Company.objects.create(
            code="CMT", name_ar="شركة الالتزامات", country="EG",
            activity="commercial", base_currency=self.currency,
        )
        self.branch = Branch.objects.create(
            company=self.company, code="MAIN", name_ar="الرئيسي", is_head_office=True,
        )
        self.year = FiscalYear.objects.create(
            company=self.company, name="2026", start_date=date(2026, 1, 1),
            end_date=date(2026, 12, 31),
        )
        self.user = get_user_model().objects.create_user(username="commitment-user", password="test-password")
        CompanyMembership.objects.create(user=self.user, company=self.company, role="accountant", all_branches=True)
        self.client.force_authenticate(self.user)
        self.payable = ChartOfAccount.objects.create(
            company=self.company, code="2110", name_ar="ضرائب مستحقة",
            nature=ChartOfAccount.Nature.LIABILITY,
        )
        self.bank = ChartOfAccount.objects.create(
            company=self.company, code="1110", name_ar="البنك",
            nature=ChartOfAccount.Nature.ASSET,
        )

    def _create_commitment(self, **overrides):
        payload = {
            "company": self.company.pk, "branch": self.branch.pk,
            "commitment_type": "tax", "title": "ضريبة قيمة مضافة",
            "period_label": "2026-10", "amount": "1000.0000",
            "currency_code": "EGP", "exchange_rate": "1",
            "due_date": "2026-11-30", "payable_account": self.payable.pk,
            "notes": "اختبار",
        }
        payload.update(overrides)
        return self.client.post("/api/treasury/commitments/", payload, format="json")

    def test_create_commitment_is_forecast_only_and_does_not_post_journal(self):
        before = JournalEntry.objects.filter(company=self.company).count()
        response = self._create_commitment()
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["base_amount"], "1000.0000")
        self.assertEqual(JournalEntry.objects.filter(company=self.company).count(), before)
        self.assertEqual(CashCommitment.objects.filter(company=self.company).count(), 1)

    def test_commitment_requires_company_liability_account_and_unique_source(self):
        invalid = self._create_commitment(payable_account=self.bank.pk)
        self.assertEqual(invalid.status_code, 400, invalid.data)
        first = self._create_commitment(origin_type="tax_return", origin_id=91)
        self.assertEqual(first.status_code, 201, first.data)
        duplicate = self._create_commitment(origin_type="tax_return", origin_id=91)
        self.assertEqual(duplicate.status_code, 400, duplicate.data)

    def test_foreign_commitment_uses_approved_due_date_rate(self):
        usd = Currency.objects.create(code="USD", name_ar="دولار أمريكي", is_active=True)
        ExchangeRate.objects.create(
            company=self.company, currency=usd, rate_date=date(2026, 11, 30),
            rate=Decimal("50"),
        )
        response = self._create_commitment(
            currency_code="USD", exchange_rate="50", amount="10",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["base_amount"], "500.0000")

    def test_payment_requires_a_dedicated_posted_journal_and_updates_remaining(self):
        response = self._create_commitment()
        self.assertEqual(response.status_code, 201, response.data)
        commitment_id = response.data["id"]
        entry = JournalEntry.objects.create(
            company=self.company, branch=self.branch, fiscal_year=self.year,
            entry_number="CMT-PAY-001", entry_date=date(2026, 11, 20),
            description="سداد جزء من الضريبة", source_type=JournalEntry.Source.MANUAL,
        )
        JournalLine.objects.bulk_create([
            JournalLine(entry=entry, account=self.payable, debit=Decimal("250"), credit=0),
            JournalLine(entry=entry, account=self.bank, debit=0, credit=Decimal("250")),
        ])
        post_journal(entry.pk)
        payment = self.client.post(
            f"/api/treasury/commitments/{commitment_id}/record-payment/",
            {"payment_date": "2026-11-20", "amount": "250", "journal_entry": entry.pk},
            format="json",
        )
        self.assertEqual(payment.status_code, 201, payment.data)
        commitment = self.client.get(f"/api/treasury/commitments/{commitment_id}/")
        self.assertEqual(commitment.data["paid_base_amount"], "250.0000")
        self.assertEqual(commitment.data["remaining_base_amount"], "750.0000")
        self.assertEqual(commitment.data["status"], "partially_paid")

    def test_recurring_schedule_generation_is_idempotent_and_uses_month_end_clamp(self):
        response = self.client.post("/api/treasury/commitment-schedules/", {
            "company": self.company.pk, "branch": self.branch.pk,
            "title": "إيجار المكتب", "amount": "5000", "currency_code": "EGP",
            "exchange_rate": "1", "frequency": "monthly",
            "next_due_date": "2026-01-31", "end_date": "2026-04-30",
            "payable_account": self.payable.pk,
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        schedule_id = response.data["id"]
        first = self.client.post(
            f"/api/treasury/commitment-schedules/{schedule_id}/generate/",
            {"through_date": "2026-04-30"}, format="json",
        )
        self.assertEqual(first.status_code, 200, first.data)
        self.assertEqual(first.data["generated"], 4)
        self.assertEqual(CashCommitmentOccurrence.objects.filter(schedule_id=schedule_id).count(), 4)
        second = self.client.post(
            f"/api/treasury/commitment-schedules/{schedule_id}/generate/",
            {"through_date": "2026-04-30"}, format="json",
        )
        self.assertEqual(second.status_code, 200, second.data)
        self.assertEqual(second.data["generated"], 0)
        self.assertEqual(CashCommitment.objects.filter(company=self.company).count(), 4)
        paused = self.client.patch(
            f"/api/treasury/commitment-schedules/{schedule_id}/",
            {"active": False}, format="json",
        )
        self.assertEqual(paused.status_code, 200, paused.data)
        self.assertFalse(paused.data["active"])
        rejected_edit = self.client.patch(
            f"/api/treasury/commitment-schedules/{schedule_id}/",
            {"amount": "6000"}, format="json",
        )
        self.assertEqual(rejected_edit.status_code, 400, rejected_edit.data)
        resumed = self.client.patch(
            f"/api/treasury/commitment-schedules/{schedule_id}/",
            {"active": True}, format="json",
        )
        self.assertEqual(resumed.status_code, 200, resumed.data)
        self.assertTrue(resumed.data["active"])

    def test_reversal_journal_restores_commitment_balance(self):
        response = self._create_commitment()
        self.assertEqual(response.status_code, 201, response.data)
        commitment_id = response.data["id"]
        original = JournalEntry.objects.create(
            company=self.company, branch=self.branch, fiscal_year=self.year,
            entry_number="CMT-PAY-REV-001", entry_date=date(2026, 11, 20),
            description="سداد الالتزام", source_type=JournalEntry.Source.MANUAL,
        )
        JournalLine.objects.bulk_create([
            JournalLine(entry=original, account=self.payable, debit=Decimal("250"), credit=0),
            JournalLine(entry=original, account=self.bank, debit=0, credit=Decimal("250")),
        ])
        post_journal(original.pk)
        payment = self.client.post(
            f"/api/treasury/commitments/{commitment_id}/record-payment/",
            {"payment_date": "2026-11-20", "amount": "250", "journal_entry": original.pk},
            format="json",
        )
        self.assertEqual(payment.status_code, 201, payment.data)
        reversal = JournalEntry.objects.create(
            company=self.company, branch=self.branch, fiscal_year=self.year,
            entry_number="CMT-PAY-REV-002", entry_date=date(2026, 11, 21),
            description="عكس سداد الالتزام", source_type=JournalEntry.Source.MANUAL,
            reversal_of=original,
        )
        JournalLine.objects.bulk_create([
            JournalLine(entry=reversal, account=self.bank, debit=Decimal("250"), credit=0),
            JournalLine(entry=reversal, account=self.payable, debit=0, credit=Decimal("250")),
        ])
        post_journal(reversal.pk)
        reversed_payment = self.client.post(
            f"/api/treasury/commitments/{commitment_id}/reverse-payment/",
            {"payment_id": payment.data["id"], "reversal_journal_entry": reversal.pk},
            format="json",
        )
        self.assertEqual(reversed_payment.status_code, 200, reversed_payment.data)
        commitment = self.client.get(f"/api/treasury/commitments/{commitment_id}/")
        self.assertEqual(commitment.data["paid_base_amount"], "0.0000")
        self.assertEqual(commitment.data["remaining_base_amount"], "1000.0000")
        self.assertEqual(commitment.data["status"], "open")
