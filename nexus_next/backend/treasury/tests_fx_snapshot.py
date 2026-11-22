from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase

from accounting.models import ChartOfAccount
from companies.models import Branch, Company, Currency, ExchangeRate, FiscalYear
from .models import TreasuryAccount, TreasuryTransaction


class TreasuryFxSnapshotTests(TestCase):
    def setUp(self):
        self.base = Currency.objects.create(code="EGP", name_ar="جنيه مصري")
        self.usd = Currency.objects.create(code="USD", name_ar="دولار أمريكي")
        self.company = Company.objects.create(
            code="FX-TREASURY-TEST", name_ar="شركة اختبار العملات",
            country="EG", activity="commercial", base_currency=self.base,
        )
        self.branch = Branch.objects.create(
            company=self.company, code="MAIN", name_ar="الرئيسي", is_head_office=True,
        )
        self.year = FiscalYear.objects.create(
            company=self.company, name="2026",
            start_date=date(2026, 1, 1), end_date=date(2026, 12, 31),
        )
        bank_ledger = ChartOfAccount.objects.create(
            company=self.company, code="1120", name_ar="بنك بالدولار", nature="asset",
        )
        self.bank = TreasuryAccount.objects.create(
            company=self.company, branch=self.branch, account=bank_ledger,
            kind=TreasuryAccount.Kind.BANK, code="USD-1", name_ar="حساب دولار",
            currency_code="USD",
        )
        self.counter = ChartOfAccount.objects.create(
            company=self.company, code="2100", name_ar="حساب مقابل", nature="liability",
        )
        ExchangeRate.objects.create(
            company=self.company, currency=self.usd,
            rate_date=date(2026, 3, 1), rate=Decimal("30"),
        )

    def make_transaction(self, rate=Decimal("30"), base_amount=Decimal("3000.0000")):
        return TreasuryTransaction(
            company=self.company, branch=self.branch, fiscal_year=self.year,
            source_account=self.bank, counter_account=self.counter,
            kind=TreasuryTransaction.Kind.RECEIPT,
            document_number="FX-RECEIPT-001", transaction_date=date(2026, 3, 1),
            amount=base_amount, transaction_currency=self.usd,
            transaction_amount=Decimal("100.0000"), exchange_rate=rate,
            exchange_rate_date=date(2026, 3, 1), description="تحصيل بالدولار",
        )

    def test_valid_snapshot_matches_company_approved_rate(self):
        transaction = self.make_transaction()
        transaction.full_clean()

    def test_unapproved_rate_is_rejected(self):
        transaction = self.make_transaction(rate=Decimal("31"), base_amount=Decimal("3100.0000"))
        with self.assertRaises(ValidationError):
            transaction.full_clean()

    def test_base_amount_must_match_foreign_amount_and_rate(self):
        transaction = self.make_transaction(base_amount=Decimal("2999.0000"))
        with self.assertRaises(ValidationError):
            transaction.full_clean()

    def test_incomplete_snapshot_is_rejected(self):
        transaction = self.make_transaction()
        transaction.exchange_rate_date = None
        with self.assertRaises(ValidationError):
            transaction.full_clean()

    def test_exchange_rate_cannot_be_from_after_transaction_date(self):
        transaction = self.make_transaction()
        transaction.exchange_rate_date = date(2026, 3, 2)
        with self.assertRaises(ValidationError):
            transaction.full_clean()
