from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase

from accounting.models import AccountRole, ChartOfAccount, JournalEntry
from companies.models import Branch, Company, Currency, ExchangeRate, FiscalYear
from .models import TreasuryAccount, TreasuryTransaction, TreasuryFxPosition, TreasuryFxRevaluation
from .services import post_treasury_transaction, revalue_treasury_fx_account


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

    def test_foreign_treasury_cannot_use_ambiguous_base_currency_opening_balance(self):
        self.bank.opening_balance = Decimal("1000")
        with self.assertRaises(ValidationError):
            self.bank.full_clean()

    def test_foreign_treasury_draft_cannot_omit_snapshot(self):
        transaction = self.make_transaction()
        transaction.transaction_currency = None
        transaction.transaction_amount = None
        transaction.exchange_rate = None
        transaction.exchange_rate_date = None
        with self.assertRaises(ValidationError):
            transaction.full_clean()

    def test_exchange_rate_cannot_be_from_after_transaction_date(self):
        transaction = self.make_transaction()
        transaction.exchange_rate_date = date(2026, 3, 2)
        with self.assertRaises(ValidationError):
            transaction.full_clean()


    def setUp_fx_roles(self):
        gain = ChartOfAccount.objects.create(
            company=self.company, code="4901", name_ar="أرباح فروق عملة",
            nature=ChartOfAccount.Nature.REVENUE,
        )
        loss = ChartOfAccount.objects.create(
            company=self.company, code="5901", name_ar="خسائر فروق عملة",
            nature=ChartOfAccount.Nature.EXPENSE,
        )
        AccountRole.objects.create(
            company=self.company, role=AccountRole.Role.REALIZED_FX_GAIN, account=gain,
        )
        AccountRole.objects.create(
            company=self.company, role=AccountRole.Role.REALIZED_FX_LOSS, account=loss,
        )
        AccountRole.objects.create(
            company=self.company, role=AccountRole.Role.UNREALIZED_FX_GAIN, account=gain,
        )
        AccountRole.objects.create(
            company=self.company, role=AccountRole.Role.UNREALIZED_FX_LOSS, account=loss,
        )
        return gain, loss

    def make_postable_transaction(self, *, kind, number, amount, rate, foreign_amount,
                                   counter=None, destination=None, rate_date=date(2026, 3, 1)):
        return TreasuryTransaction.objects.create(
            company=self.company, branch=self.branch, fiscal_year=self.year,
            source_account=self.bank, counter_account=counter or self.counter,
            destination_account=destination, kind=kind,
            document_number=number, transaction_date=rate_date,
            amount=amount, transaction_currency=self.usd,
            transaction_amount=foreign_amount, exchange_rate=rate,
            exchange_rate_date=rate_date, description=number,
        )

    def test_foreign_receipt_and_payment_use_average_carrying_value_and_realized_fx(self):
        _, loss = self.setUp_fx_roles()
        self.make_postable_transaction(
            kind=TreasuryTransaction.Kind.RECEIPT, number="FX-R-1",
            amount=Decimal("3000"), rate=Decimal("30"),
            foreign_amount=Decimal("100"),
        )
        receipt = TreasuryTransaction.objects.get(document_number="FX-R-1")
        post_treasury_transaction(receipt.pk)
        receipt.refresh_from_db()
        receipt_bank_line = receipt.journal_entry.lines.get(account=self.bank.account)
        self.assertEqual(receipt_bank_line.transaction_amount, Decimal("100.0000"))
        self.assertEqual(receipt_bank_line.exchange_rate, Decimal("30.0000000000"))
        receipt_counter_line = receipt.journal_entry.lines.get(account=self.counter)
        self.assertEqual(receipt_counter_line.transaction_amount, Decimal("100.0000"))
        position = TreasuryFxPosition.objects.get(account=self.bank)
        self.assertEqual(position.foreign_amount, Decimal("100"))
        self.assertEqual(position.carrying_base_amount, Decimal("3000.0000"))

        ExchangeRate.objects.create(
            company=self.company, currency=self.usd,
            rate_date=date(2026, 3, 2), rate=Decimal("32"),
        )
        revaluation = revalue_treasury_fx_account(
            account_id=self.bank.pk, fiscal_year_id=self.year.pk,
            revaluation_date=date(2026, 3, 2), rate_date=date(2026, 3, 2),
            branch=self.branch,
        )
        self.assertEqual(revaluation.difference, Decimal("200.0000"))
        self.assertIsNotNone(revaluation.journal_entry_id)
        position.refresh_from_db()
        self.assertEqual(position.carrying_base_amount, Decimal("3200.0000"))

        ExchangeRate.objects.create(
            company=self.company, currency=self.usd,
            rate_date=date(2026, 3, 3), rate=Decimal("31"),
        )
        payment = self.make_postable_transaction(
            kind=TreasuryTransaction.Kind.PAYMENT, number="FX-P-1",
            amount=Decimal("1550"), rate=Decimal("31"),
            foreign_amount=Decimal("50"), rate_date=date(2026, 3, 3),
        )
        post_treasury_transaction(payment.pk)
        payment.refresh_from_db()
        payment_counter_line = payment.journal_entry.lines.get(account=self.counter)
        self.assertEqual(payment_counter_line.transaction_amount, Decimal("50.0000"))
        payment_bank_line = payment.journal_entry.lines.get(account=self.bank.account)
        self.assertIsNone(payment_bank_line.transaction_amount)
        payment.refresh_from_db()
        self.assertEqual(payment.carrying_amount, Decimal("1600.0000"))
        position.refresh_from_db()
        self.assertEqual(position.foreign_amount, Decimal("50.0000"))
        self.assertEqual(position.carrying_base_amount, Decimal("1600.0000"))
        self.assertEqual(
            payment.journal_entry.lines.get(account=loss).debit,
            Decimal("50.0000"),
        )

    def test_foreign_payment_reduces_position_when_realized_fx_difference_is_zero(self):
        receipt = self.make_postable_transaction(
            kind=TreasuryTransaction.Kind.RECEIPT, number="FX-R-ZERO",
            amount=Decimal("3000"), rate=Decimal("30"),
            foreign_amount=Decimal("100"),
        )
        post_treasury_transaction(receipt.pk)
        payment = self.make_postable_transaction(
            kind=TreasuryTransaction.Kind.PAYMENT, number="FX-P-ZERO",
            amount=Decimal("1500"), rate=Decimal("30"),
            foreign_amount=Decimal("50"),
        )
        post_treasury_transaction(payment.pk)
        payment.refresh_from_db()
        position = TreasuryFxPosition.objects.get(account=self.bank)
        self.assertEqual(position.foreign_amount, Decimal("50.0000"))
        self.assertEqual(position.carrying_base_amount, Decimal("1500.0000"))
        self.assertEqual(payment.journal_entry.lines.filter(account=self.bank.account).get().credit,
                         Decimal("1500.0000"))

    def test_foreign_payment_cannot_overdraw_nominal_currency_position(self):
        self.setUp_fx_roles()
        payment = self.make_postable_transaction(
            kind=TreasuryTransaction.Kind.PAYMENT, number="FX-P-OVER",
            amount=Decimal("3000"), rate=Decimal("30"),
            foreign_amount=Decimal("100"),
        )
        with self.assertRaises(ValidationError):
            post_treasury_transaction(payment.pk)
        payment.refresh_from_db()
        self.assertEqual(payment.status, TreasuryTransaction.Status.DRAFT)
        self.assertFalse(JournalEntry.objects.filter(entry_number=f"TR-{payment.pk}").exists())

    def test_revaluation_rejects_date_before_latest_posted_treasury_movement(self):
        self.setUp_fx_roles()
        self.make_postable_transaction(
            kind=TreasuryTransaction.Kind.RECEIPT, number="FX-R-LATE",
            amount=Decimal("3200"), rate=Decimal("32"),
            foreign_amount=Decimal("100"), rate_date=date(2026, 3, 2),
        )
        receipt = TreasuryTransaction.objects.get(document_number="FX-R-LATE")
        post_treasury_transaction(receipt.pk)
        with self.assertRaises(ValidationError):
            revalue_treasury_fx_account(
                account_id=self.bank.pk, fiscal_year_id=self.year.pk,
                revaluation_date=date(2026, 3, 1), rate_date=date(2026, 3, 1),
                branch=self.branch,
            )
        self.assertFalse(TreasuryFxRevaluation.objects.filter(account=self.bank).exists())

    def test_backdated_or_same_day_movement_is_rejected_after_revaluation(self):
        self.setUp_fx_roles()
        self.make_postable_transaction(
            kind=TreasuryTransaction.Kind.RECEIPT, number="FX-R-BEFORE-REV",
            amount=Decimal("3000"), rate=Decimal("30"),
            foreign_amount=Decimal("100"),
        )
        receipt = TreasuryTransaction.objects.get(document_number="FX-R-BEFORE-REV")
        post_treasury_transaction(receipt.pk)
        ExchangeRate.objects.create(
            company=self.company, currency=self.usd,
            rate_date=date(2026, 3, 2), rate=Decimal("32"),
        )
        revalue_treasury_fx_account(
            account_id=self.bank.pk, fiscal_year_id=self.year.pk,
            revaluation_date=date(2026, 3, 2), rate_date=date(2026, 3, 2),
            branch=self.branch,
        )
        backdated = self.make_postable_transaction(
            kind=TreasuryTransaction.Kind.RECEIPT, number="FX-R-BACKDATED",
            amount=Decimal("3000"), rate=Decimal("30"),
            foreign_amount=Decimal("100"),
        )
        with self.assertRaises(ValidationError):
            post_treasury_transaction(backdated.pk)

    def test_revaluation_cannot_be_repeated_for_same_account_and_date(self):
        self.setUp_fx_roles()
        self.make_postable_transaction(
            kind=TreasuryTransaction.Kind.RECEIPT, number="FX-R-REVAL",
            amount=Decimal("3000"), rate=Decimal("30"),
            foreign_amount=Decimal("100"),
        )
        receipt = TreasuryTransaction.objects.get(document_number="FX-R-REVAL")
        post_treasury_transaction(receipt.pk)
        ExchangeRate.objects.create(
            company=self.company, currency=self.usd,
            rate_date=date(2026, 3, 2), rate=Decimal("32"),
        )
        kwargs = dict(
            account_id=self.bank.pk, fiscal_year_id=self.year.pk,
            revaluation_date=date(2026, 3, 2), rate_date=date(2026, 3, 2),
            branch=self.branch,
        )
        revalue_treasury_fx_account(**kwargs)
        with self.assertRaises(ValidationError):
            revalue_treasury_fx_account(**kwargs)
        self.assertEqual(TreasuryFxRevaluation.objects.filter(account=self.bank).count(), 1)
