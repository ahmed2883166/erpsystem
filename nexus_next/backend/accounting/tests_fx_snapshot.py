from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase

from companies.models import Branch, Company, Currency, FiscalYear
from accounting.models import AccountRole, ChartOfAccount, JournalEntry, JournalLine
from accounting.serializers import AccountRoleSerializer


class JournalLineFxSnapshotTests(TestCase):
    def setUp(self):
        self.base = Currency.objects.create(code="EGP", name_ar="جنيه مصري", decimal_places=2)
        self.usd = Currency.objects.create(code="USD", name_ar="دولار أمريكي", decimal_places=2)
        self.company = Company.objects.create(
            code="FX-TEST", name_ar="شركة اختبار العملات", country=Company.Country.EGYPT,
            activity=Company.Activity.COMMERCIAL, base_currency=self.base,
        )
        self.branch = Branch.objects.create(company=self.company, code="MAIN", name_ar="الرئيسي")
        self.year = FiscalYear.objects.create(
            company=self.company, name="2026", start_date=date(2026, 1, 1),
            end_date=date(2026, 12, 31), status=FiscalYear.Status.OPEN,
        )
        self.account = ChartOfAccount.objects.create(
            company=self.company, code="1100", name_ar="بنك", nature=ChartOfAccount.Nature.ASSET,
        )
        self.entry = JournalEntry.objects.create(
            company=self.company, branch=self.branch, fiscal_year=self.year,
            entry_number="FX-0001", entry_date=date(2026, 3, 2), description="اختبار عملة أجنبية",
        )
        from companies.models import ExchangeRate
        ExchangeRate.objects.create(
            company=self.company, currency=self.usd, rate_date=date(2026, 3, 1),
            rate=Decimal("30.0000000000"), source="اختبار",
        )

    def make_line(self, **overrides):
        values = {
            "entry": self.entry, "account": self.account, "debit": Decimal("3000.0000"),
            "credit": Decimal("0"), "transaction_currency": self.usd,
            "transaction_amount": Decimal("100.0000"), "exchange_rate": Decimal("30.0000000000"),
            "exchange_rate_date": date(2026, 3, 1),
        }
        values.update(overrides)
        return JournalLine(**values)

    def test_valid_foreign_currency_snapshot_matches_base_ledger_amount(self):
        line = self.make_line()
        line.full_clean()

    def test_incomplete_foreign_currency_snapshot_is_rejected(self):
        line = self.make_line(exchange_rate=None)
        with self.assertRaises(ValidationError):
            line.full_clean()

    def test_future_exchange_rate_date_is_rejected(self):
        line = self.make_line(exchange_rate_date=date(2026, 3, 3))
        with self.assertRaises(ValidationError):
            line.full_clean()

    def test_base_equivalent_must_match_foreign_amount_times_rate(self):
        line = self.make_line(debit=Decimal("3001.0000"))
        with self.assertRaises(ValidationError):
            line.full_clean()

    def test_base_currency_snapshot_requires_rate_one(self):
        line = self.make_line(
            transaction_currency=self.base, transaction_amount=Decimal("100.0000"),
            exchange_rate=Decimal("30.0000000000"),
        )
        with self.assertRaises(ValidationError):
            line.full_clean()

    def test_snapshot_rate_must_match_approved_rate_for_exact_date(self):
        line = self.make_line(exchange_rate=Decimal("31.0000000000"))
        with self.assertRaises(ValidationError):
            line.full_clean()

    def test_snapshot_date_must_reference_an_approved_rate_record(self):
        line = self.make_line(exchange_rate_date=date(2026, 2, 28))
        with self.assertRaises(ValidationError):
            line.full_clean()

    def test_fx_gain_and_loss_roles_require_correct_account_nature(self):
        revenue = ChartOfAccount.objects.create(
            company=self.company, code="4000", name_ar="أرباح فروق العملة",
            nature=ChartOfAccount.Nature.REVENUE,
        )
        expense = ChartOfAccount.objects.create(
            company=self.company, code="5000", name_ar="خسائر فروق العملة",
            nature=ChartOfAccount.Nature.EXPENSE,
        )
        gain = AccountRoleSerializer(data={
            "company": self.company.pk, "role": AccountRole.Role.REALIZED_FX_GAIN,
            "account": revenue.pk,
        })
        self.assertTrue(gain.is_valid(), gain.errors)
        invalid_loss = AccountRoleSerializer(data={
            "company": self.company.pk, "role": AccountRole.Role.REALIZED_FX_LOSS,
            "account": revenue.pk,
        })
        self.assertFalse(invalid_loss.is_valid())
