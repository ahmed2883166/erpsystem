from datetime import date
from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import User
from companies.models import Branch, Company, CompanyMembership, Currency
from accounting.models import ChartOfAccount
from .models import BankReconciliation, TreasuryAccount


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
