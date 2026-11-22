from datetime import date
from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import User
from companies.models import Branch, Company, CompanyMembership, Currency, FiscalYear
from accounting.models import ChartOfAccount, JournalEntry, JournalLine
from .models import BankReconciliation, TreasuryAccount, TreasuryTransaction
from .services import record_posted_document_bank_movement


class BankReconciliationOpeningBalanceTests(TestCase):
    def setUp(self):
        currency = Currency.objects.create(code="EGP", name_ar="جنيه مصري")
        self.company = Company.objects.create(
            code="BANK-TEST", name_ar="شركة اختبار البنك", country="EG",
            activity="commercial", base_currency=currency,
        )
        self.branch = Branch.objects.create(
            company=self.company, code="MAIN", name_ar="الرئيسي", is_head_office=True,
        )
        account = ChartOfAccount.objects.create(
            company=self.company, code="1100", name_ar="البنك", nature="asset",
        )
        self.bank = TreasuryAccount.objects.create(
            company=self.company, branch=self.branch, account=account,
            kind=TreasuryAccount.Kind.BANK, code="BANK-1", name_ar="الحساب البنكي",
            opening_balance=Decimal("1000.0000"),
        )
        self.reconciliation = BankReconciliation.objects.create(
            company=self.company, bank_account=self.bank, statement_number="ST-001",
            statement_date=date(2026, 1, 31), opening_balance=Decimal("1000.0000"),
            closing_balance=Decimal("1000.0000"),
        )
        self.client = APIClient()

    def test_summary_includes_treasury_account_opening_balance(self):
        user = User.objects.create_user(username="bank_viewer", password="test-password")
        CompanyMembership.objects.create(
            user=user, company=self.company, role="viewer", all_branches=True,
        )
        self.client.force_authenticate(user=user)
        response = self.client.get(
            f"/api/treasury/reconciliations/{self.reconciliation.pk}/summary/"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["book_balance"], "1000.0000")
        self.assertEqual(response.data["expected_book_balance"], "1000.0000")
        self.assertEqual(response.data["unexplained_difference"], "0.0000")

    def test_sales_and_purchase_bank_mirrors_are_visible_as_outstanding_items(self):
        year = FiscalYear.objects.create(
            company=self.company, name="2026", start_date=date(2026, 1, 1), end_date=date(2026, 12, 31),
        )
        counter = ChartOfAccount.objects.create(
            company=self.company, code="2100", name_ar="الموردون", nature="liability",
        )
        entry = JournalEntry.objects.create(
            company=self.company, branch=self.branch, fiscal_year=year,
            entry_number="BANK-PAY-001", entry_date=date(2026, 1, 20),
            description="سداد مورد", source_type=JournalEntry.Source.PURCHASES,
            status=JournalEntry.Status.POSTED,
        )
        JournalLine.objects.bulk_create([
            JournalLine(entry=entry, account=counter, debit=Decimal("100"), credit=0),
            JournalLine(entry=entry, account=self.bank.account, debit=0, credit=Decimal("100")),
        ])
        record_posted_document_bank_movement(
            company=self.company, branch=self.branch, fiscal_year=year, account=self.bank.account,
            kind=TreasuryTransaction.Kind.PAYMENT, amount=Decimal("100"),
            transaction_date=date(2026, 1, 20), document_number="AUTO-PUR-PAY-1",
            description="سداد فاتورة مورد", counter_account=counter, journal_entry=entry,
        )
        user = User.objects.create_user(username="bank_mirror_viewer", password="test-password")
        CompanyMembership.objects.create(
            user=user, company=self.company, role="viewer", all_branches=True,
        )
        self.client.force_authenticate(user=user)
        response = self.client.get(
            f"/api/treasury/reconciliations/{self.reconciliation.pk}/summary/"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["outstanding_total"], "-100.0000")
        self.assertEqual(response.data["book_balance"], "900.0000")
        self.assertEqual(response.data["expected_book_balance"], "900.0000")
        self.assertEqual(response.data["unexplained_difference"], "0.0000")

    def test_unmapped_bank_account_does_not_block_posted_document(self):
        year = FiscalYear.objects.create(
            company=self.company, name="2026", start_date=date(2026, 1, 1), end_date=date(2026, 12, 31),
        )
        unmapped_bank = ChartOfAccount.objects.create(
            company=self.company, code="1199", name_ar="بنك غير مربوط", nature="asset",
        )
        counter = ChartOfAccount.objects.create(
            company=self.company, code="2199", name_ar="حساب مقابل", nature="liability",
        )
        entry = JournalEntry.objects.create(
            company=self.company, branch=self.branch, fiscal_year=year,
            entry_number="BANK-UNMAPPED-001", entry_date=date(2026, 1, 20),
            description="تحصيل بنكي", source_type=JournalEntry.Source.SALES,
            status=JournalEntry.Status.POSTED,
        )

        mirrored = record_posted_document_bank_movement(
            company=self.company, branch=self.branch, fiscal_year=year, account=unmapped_bank,
            kind=TreasuryTransaction.Kind.RECEIPT, amount=Decimal("25"),
            transaction_date=date(2026, 1, 20), document_number="AUTO-SALE-UNMAPPED-1",
            description="تحصيل عميل", counter_account=counter, journal_entry=entry,
        )

        self.assertIsNone(mirrored)
        self.assertFalse(TreasuryTransaction.objects.filter(
            company=self.company, document_number="AUTO-SALE-UNMAPPED-1",
        ).exists())

    def test_reconciliation_can_be_approved_when_opening_balances_match(self):
        user = User.objects.create_user(username="bank_accountant", password="test-password")
        CompanyMembership.objects.create(
            user=user, company=self.company, role="accountant", all_branches=True,
        )
        self.client.force_authenticate(user=user)
        response = self.client.post(
            f"/api/treasury/reconciliations/{self.reconciliation.pk}/reconcile/",
            {}, format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.reconciliation.refresh_from_db()
        self.assertEqual(self.reconciliation.status, BankReconciliation.Status.RECONCILED)
