"""نماذج الخزائن والحسابات البنكية والحركات المالية ومطابقة كشوف البنوك."""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from decimal import Decimal

from companies.models import Company, Branch, Currency, ExchangeRate
from core.money import round_currency_amount
from accounting.models import ChartOfAccount, JournalEntry


class TreasuryAccount(models.Model):
    """يمثل خزينة أو حسابًا بنكيًا مستقلًا داخل الشركة والفرع."""
    class Kind(models.TextChoices):
        CASH = "cash", "خزينة"
        BANK = "bank", "بنك"

    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="treasury_accounts")
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name="treasury_accounts", null=True, blank=True)
    account = models.OneToOneField(ChartOfAccount, on_delete=models.PROTECT, related_name="treasury_account")
    kind = models.CharField(max_length=8, choices=Kind.choices)
    code = models.CharField(max_length=32)
    name_ar = models.CharField(max_length=160)
    bank_name = models.CharField(max_length=160, blank=True)
    account_number = models.CharField(max_length=80, blank=True)
    iban = models.CharField(max_length=64, blank=True)
    currency_code = models.CharField(max_length=3, blank=True, default="")
    opening_balance = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    opening_journal_entry = models.OneToOneField(
        JournalEntry, null=True, blank=True, on_delete=models.PROTECT,
        related_name="treasury_account_opening",
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("kind", "code")
        constraints = [models.UniqueConstraint(fields=("company", "code"), name="uq_treasury_account_company_code")]
        indexes = [models.Index(fields=("company", "kind", "is_active"), name="tr_co_kind_active_idx")]

    @property
    def company_id_for_permission(self):
        return self.company_id

    def clean(self):
        super().clean()
        if self.branch_id and self.branch.company_id != self.company_id:
            raise ValidationError({"branch": "الفرع لا يتبع الشركة المحددة."})
        if self.account_id and self.account.company_id != self.company_id:
            raise ValidationError({"account": "الحساب المحاسبي لا يتبع الشركة المحددة."})
        if self.account_id and (self.account.is_group or not self.account.allow_posting or not self.account.is_active):
            raise ValidationError({"account": "اختر حسابًا تفصيليًا نشطًا قابلًا للترحيل."})
        if self.opening_balance < 0:
            raise ValidationError({"opening_balance": "الرصيد الافتتاحي لا يمكن أن يكون سالبًا."})
        if self.company_id and self.currency_code != self.company.base_currency.code and self.opening_balance > 0:
            raise ValidationError({"opening_balance": "الرصيد الافتتاحي الأجنبي يحتاج مبلغًا اسميًا وسعر صرف معتمدًا؛ لا تستخدم قيمة أساسية غير محددة."})

    def save(self, *args, **kwargs):
        if not self.currency_code and self.company_id:
            self.currency_code = self.company.base_currency.code
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.opening_journal_entry_id:
            raise ValidationError("لا يمكن حذف خزينة مرتبطة بقيد افتتاحي؛ عطّل الحساب للحفاظ على الأثر المحاسبي.")
        if TreasuryTransaction.objects.filter(models.Q(source_account=self) | models.Q(destination_account=self)).exists():
            raise ValidationError("لا يمكن حذف خزينة لها حركات؛ عطّل الحساب بدلًا من حذفها.")
        if TreasuryFxPosition.objects.filter(account=self).exists() or TreasuryFxRevaluation.objects.filter(account=self).exists():
            raise ValidationError("لا يمكن حذف حساب خزينة له مركز عملة أو إعادة تقييم؛ عطّل الحساب للحفاظ على سجل فروق العملة.")
        return super().delete(*args, **kwargs)

    def __str__(self):
        return f"{self.code} - {self.name_ar}"


class TreasuryTransaction(models.Model):
    """يمثل حركة قبض أو صرف مرتبطة بحساب الخزينة."""
    class Kind(models.TextChoices):
        RECEIPT = "receipt", "سند قبض"
        PAYMENT = "payment", "سند صرف"
        TRANSFER = "transfer", "تحويل بين الخزائن"

    class Status(models.TextChoices):
        DRAFT = "draft", "مسودة"
        POSTED = "posted", "مرحّل"
        VOID = "void", "ملغي"

    class CashFlowCategory(models.TextChoices):
        OPERATING = "operating", "تشغيلي"
        INVESTING = "investing", "استثماري"
        FINANCING = "financing", "تمويلي"

    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="treasury_transactions")
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name="treasury_transactions")
    fiscal_year = models.ForeignKey("companies.FiscalYear", on_delete=models.PROTECT, related_name="treasury_transactions")
    source_account = models.ForeignKey(TreasuryAccount, on_delete=models.PROTECT, related_name="outgoing_transactions")
    destination_account = models.ForeignKey(TreasuryAccount, null=True, blank=True, on_delete=models.PROTECT, related_name="incoming_transfers")
    counter_account = models.ForeignKey(ChartOfAccount, null=True, blank=True, on_delete=models.PROTECT, related_name="treasury_counter_transactions")
    reversal_of = models.OneToOneField(
        "self", null=True, blank=True, on_delete=models.PROTECT,
        related_name="reversal_transaction",
    )
    kind = models.CharField(max_length=10, choices=Kind.choices)
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.DRAFT)
    document_number = models.CharField(max_length=40)
    transaction_date = models.DateField()
    # amount remains the company-base-currency carrying amount for legacy posting.
    # FX drafts additionally retain the original nominal amount and approved rate.
    amount = models.DecimalField(max_digits=20, decimal_places=4)
    transaction_currency = models.ForeignKey(
        Currency, null=True, blank=True, on_delete=models.PROTECT,
        related_name="treasury_transactions",
    )
    transaction_amount = models.DecimalField(max_digits=20, decimal_places=4, null=True, blank=True)
    exchange_rate = models.DecimalField(max_digits=20, decimal_places=10, null=True, blank=True)
    exchange_rate_date = models.DateField(null=True, blank=True)
    carrying_amount = models.DecimalField(max_digits=20, decimal_places=4, null=True, blank=True)
    description = models.CharField(max_length=500)
    reference = models.CharField(max_length=120, blank=True)
    cash_flow_category = models.CharField(max_length=12, choices=CashFlowCategory.choices, default=CashFlowCategory.OPERATING)
    journal_entry = models.OneToOneField(JournalEntry, null=True, blank=True, on_delete=models.PROTECT, related_name="treasury_transaction")
    created_by = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="created_treasury_transactions")
    created_at = models.DateTimeField(auto_now_add=True)
    posted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-transaction_date", "-id")
        constraints = [models.UniqueConstraint(fields=("company", "document_number"), name="uq_treasury_doc_company_no")]
        indexes = [models.Index(fields=("company", "status", "transaction_date"), name="tr_co_status_date_idx")]

    @property
    def company_id_for_permission(self):
        return self.company_id

    def clean(self):
        super().clean()
        if self.branch_id and self.branch.company_id != self.company_id:
            raise ValidationError({"branch": "الفرع لا يتبع الشركة."})
        if self.fiscal_year_id and self.fiscal_year.company_id != self.company_id:
            raise ValidationError({"fiscal_year": "السنة المالية لا تتبع الشركة."})
        if self.source_account_id and self.source_account.company_id != self.company_id:
            raise ValidationError({"source_account": "حساب الخزينة لا يتبع الشركة."})
        if self.destination_account_id and self.destination_account.company_id != self.company_id:
            raise ValidationError({"destination_account": "الخزينة المستقبلة لا تتبع الشركة."})
        if self.counter_account_id and self.counter_account.company_id != self.company_id:
            raise ValidationError({"counter_account": "الحساب المقابل لا يتبع الشركة."})
        if self.amount <= 0:
            raise ValidationError({"amount": "يجب أن تكون القيمة أكبر من صفر."})
        fx_values = (
            self.transaction_currency_id, self.transaction_amount,
            self.exchange_rate, self.exchange_rate_date,
        )
        foreign_account_currency = (
            self.source_account.currency_code
            if self.source_account_id else self.company.base_currency.code
        )
        if foreign_account_currency != self.company.base_currency.code and not all(
            value is not None for value in fx_values
        ):
            raise ValidationError("حساب الخزينة بعملة أجنبية؛ يجب إدخال المبلغ الأجنبي وسعر الصرف المعتمد كاملًا.")
        if any(value is not None for value in fx_values):
            if any(value is None for value in fx_values):
                raise ValidationError("بيانات العملة الأجنبية وسعر الصرف يجب إدخالها كاملة.")
            if not self.transaction_currency.is_active:
                raise ValidationError({"transaction_currency": "العملة المختارة موقوفة."})
            if self.transaction_currency.code == self.company.base_currency.code:
                raise ValidationError({"transaction_currency": "استخدم العملة الأساسية دون بيانات تحويل أجنبي."})
            if self.source_account_id and self.source_account.currency_code != self.transaction_currency.code:
                raise ValidationError({"transaction_currency": "عملة المعاملة لا تطابق عملة حساب الخزينة."})
            if self.transaction_amount <= 0 or self.exchange_rate <= 0:
                raise ValidationError("المبلغ الأجنبي وسعر الصرف يجب أن يكونا أكبر من صفر.")
            if self.exchange_rate_date > self.transaction_date:
                raise ValidationError({"exchange_rate_date": "تاريخ سعر الصرف لا يجوز أن يتجاوز تاريخ الحركة."})
            approved = ExchangeRate.objects.filter(
                company=self.company, currency=self.transaction_currency,
                rate_date=self.exchange_rate_date, rate=self.exchange_rate,
            ).exists()
            if not approved:
                raise ValidationError("سعر الصرف لا يطابق سعرًا معتمدًا مسجلًا للشركة والتاريخ.")
            expected_base = round_currency_amount(self.transaction_amount * self.exchange_rate, self.company.base_currency)
            if self.amount != expected_base:
                raise ValidationError({"amount": "القيمة بالعملة الأساسية لا تطابق المبلغ الأجنبي مضروبًا في سعر الصرف."})
        if self.reversal_of_id:
            original = self.reversal_of
            if (
                self.kind != self.Kind.RECEIPT
                or original.kind != self.Kind.PAYMENT
                or original.status != self.Status.POSTED
                or original.company_id != self.company_id
                or original.source_account_id != self.source_account_id
                or original.amount != self.amount
                or original.transaction_currency_id != self.transaction_currency_id
                or original.transaction_amount != self.transaction_amount
                or original.exchange_rate != self.exchange_rate
                or original.exchange_rate_date != self.exchange_rate_date
            ):
                raise ValidationError("حركة عكس العملة الأجنبية يجب أن تطابق سند الصرف الأصلي.")
        if self.kind == self.Kind.TRANSFER:
            if not self.destination_account_id or self.counter_account_id:
                raise ValidationError("التحويل يتطلب خزينة مستقبلة ولا يقبل حسابًا مقابلًا.")
            if self.destination_account_id == self.source_account_id:
                raise ValidationError({"destination_account": "لا يمكن التحويل إلى نفس الخزينة."})
            if self.destination_account.currency_code != self.source_account.currency_code:
                raise ValidationError({"destination_account": "التحويل يتطلب نفس العملة حاليًا."})
        elif not self.counter_account_id or self.destination_account_id:
            raise ValidationError("سند القبض أو الصرف يتطلب حسابًا مقابلًا فقط.")

    def save(self, *args, **kwargs):
        if self.pk:
            old = type(self).objects.filter(pk=self.pk).first()
            if old and old.status == self.Status.POSTED:
                raise ValidationError("المستند المرحّل غير قابل للتعديل.")
            if old:
                from hr.models import PayrollPosting
                if PayrollPosting.objects.filter(payment_treasury_transaction_id=self.pk).exists():
                    immutable_fields = (
                        "company_id", "branch_id", "fiscal_year_id", "source_account_id",
                        "destination_account_id", "counter_account_id", "kind", "document_number",
                        "transaction_date", "amount", "description", "reference", "cash_flow_category",
                        "created_by_id",
                    )
                    if any(getattr(old, field) != getattr(self, field) for field in immutable_fields):
                        raise ValidationError("مستند صرف المرتبات مرتبط بدورة مرتبات ولا يمكن تغيير بياناته؛ ألغِ إعداد الصرف من دورة المرتبات.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        from hr.models import PayrollPosting
        if PayrollPosting.objects.filter(payment_treasury_transaction_id=self.pk).exists():
            raise ValidationError("مستند صرف المرتبات مرتبط بدورة مرتبات ولا يمكن حذفه؛ ألغِ إعداد الصرف من دورة المرتبات.")
        if self.status != self.Status.DRAFT:
            raise ValidationError("لا يمكن حذف مستند خزينة مرحّل.")
        return super().delete(*args, **kwargs)


class TreasuryFxPosition(models.Model):
    """الرصيد الاسمي والقيمة الدفترية الحالية لحساب خزينة أجنبي."""
    account = models.OneToOneField(
        TreasuryAccount, on_delete=models.PROTECT, related_name="fx_position",
    )
    foreign_amount = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    carrying_base_amount = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(foreign_amount__gte=0), name="ck_treasury_fx_position_foreign_nonnegative"),
            models.CheckConstraint(condition=models.Q(carrying_base_amount__gte=0), name="ck_treasury_fx_position_base_nonnegative"),
        ]

    def clean(self):
        super().clean()
        if self.account_id and self.account.currency_code == self.account.company.base_currency.code:
            raise ValidationError({"account": "مركز العملة متاح لحسابات الخزينة الأجنبية فقط."})


class TreasuryFxRevaluation(models.Model):
    """سجل إعادة تقييم مركز عملة أجنبية وربطه بقيد اليومية."""
    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="treasury_fx_revaluations")
    account = models.ForeignKey(TreasuryAccount, on_delete=models.PROTECT, related_name="fx_revaluations")
    fiscal_year = models.ForeignKey("companies.FiscalYear", on_delete=models.PROTECT, related_name="treasury_fx_revaluations")
    revaluation_date = models.DateField()
    rate_date = models.DateField()
    rate = models.DecimalField(max_digits=20, decimal_places=10)
    foreign_amount = models.DecimalField(max_digits=20, decimal_places=4)
    previous_carrying_amount = models.DecimalField(max_digits=20, decimal_places=4)
    new_carrying_amount = models.DecimalField(max_digits=20, decimal_places=4)
    difference = models.DecimalField(max_digits=20, decimal_places=4)
    journal_entry = models.OneToOneField(
        JournalEntry, null=True, blank=True, on_delete=models.PROTECT, related_name="treasury_fx_revaluation",
    )
    reversal_journal_entry = models.OneToOneField(
        JournalEntry, null=True, blank=True, on_delete=models.PROTECT,
        related_name="treasury_fx_revaluation_reversal",
    )
    reversed_at = models.DateTimeField(null=True, blank=True)
    reversed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
        related_name="treasury_fx_revaluations_reversed",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-revaluation_date", "-id")
        constraints = [
            models.UniqueConstraint(fields=("account", "revaluation_date"), name="uq_treasury_fx_revaluation_account_date"),
            models.CheckConstraint(condition=models.Q(rate__gt=0), name="ck_treasury_fx_revaluation_rate_positive"),
            models.CheckConstraint(condition=models.Q(foreign_amount__gte=0), name="ck_treasury_fx_revaluation_foreign_nonnegative"),
        ]
        indexes = [models.Index(fields=("company", "revaluation_date"), name="tr_fx_rev_co_date_idx")]


class BankReconciliation(models.Model):
    """يمثل جلسة مطابقة رصيد البنك مع الحركات الدفترية."""
    class Status(models.TextChoices):
        DRAFT = "draft", "مسودة"
        RECONCILED = "reconciled", "تمت التسوية"

    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="bank_reconciliations")
    bank_account = models.ForeignKey(TreasuryAccount, on_delete=models.PROTECT, related_name="reconciliations")
    statement_number = models.CharField(max_length=60)
    statement_date = models.DateField()
    opening_balance = models.DecimalField(max_digits=20, decimal_places=4)
    closing_balance = models.DecimalField(max_digits=20, decimal_places=4)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT)
    created_by = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="created_bank_reconciliations")
    created_at = models.DateTimeField(auto_now_add=True)
    reconciled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-statement_date", "-id")
        constraints = [models.UniqueConstraint(fields=("company", "bank_account", "statement_number"), name="uq_bank_recon_statement_no")]

    @property
    def company_id_for_permission(self):
        return self.company_id

    def clean(self):
        super().clean()
        if self.bank_account_id and self.bank_account.company_id != self.company_id:
            raise ValidationError({"bank_account": "الحساب البنكي لا يتبع الشركة."})
        if self.bank_account_id and self.bank_account.kind != TreasuryAccount.Kind.BANK:
            raise ValidationError({"bank_account": "التسوية متاحة للحسابات البنكية فقط."})
class BankStatementLine(models.Model):
    """يمثل حركة واردة من كشف البنك أثناء المطابقة."""
    reconciliation = models.ForeignKey(BankReconciliation, on_delete=models.CASCADE, related_name="lines")
    line_number = models.PositiveIntegerField()
    transaction_date = models.DateField()
    description = models.CharField(max_length=500, blank=True)
    reference = models.CharField(max_length=120, blank=True)
    amount = models.DecimalField(max_digits=20, decimal_places=4)
    matched_transaction = models.ForeignKey(TreasuryTransaction, null=True, blank=True, on_delete=models.PROTECT, related_name="bank_statement_matches")
    matched_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("line_number", "id")
        constraints = [
            models.UniqueConstraint(fields=("reconciliation", "line_number"), name="uq_bank_recon_line_no"),
            models.UniqueConstraint(fields=("reconciliation", "matched_transaction"), condition=models.Q(matched_transaction__isnull=False), name="uq_bank_recon_matched_tx"),
        ]
