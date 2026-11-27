"""نماذج الخزائن والحسابات البنكية والحركات المالية ومطابقة كشوف البنوك."""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from decimal import Decimal

from companies.models import Company, Branch, Currency, ExchangeRate
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
    opening_foreign_amount = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    opening_exchange_rate = models.DecimalField(max_digits=20, decimal_places=10, null=True, blank=True)
    opening_exchange_rate_date = models.DateField(null=True, blank=True)
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
        if self.opening_balance < 0 or self.opening_foreign_amount < 0:
            raise ValidationError({"opening_balance": "الرصيد الافتتاحي لا يمكن أن يكون سالبًا."})
        if self.company_id:
            is_foreign = self.currency_code != self.company.base_currency.code
            fx_values = (self.opening_foreign_amount, self.opening_exchange_rate, self.opening_exchange_rate_date)
            supplied_fx_opening = (
                self.opening_foreign_amount > 0
                or self.opening_exchange_rate is not None
                or self.opening_exchange_rate_date is not None
            )
            if is_foreign and supplied_fx_opening:
                if not all(value is not None for value in fx_values) or self.opening_foreign_amount <= 0 or self.opening_exchange_rate <= 0 or self.opening_balance <= 0:
                    raise ValidationError("الرصيد الافتتاحي الأجنبي يتطلب المبلغ الأجنبي والقيمة الأساسية وسعر الصرف وتاريخه.")
                approved = ExchangeRate.objects.filter(
                    company=self.company, currency__code=self.currency_code,
                    rate_date=self.opening_exchange_rate_date, rate=self.opening_exchange_rate,
                ).exists()
                if not approved:
                    raise ValidationError("سعر الرصيد الافتتاحي لا يطابق سعرًا معتمدًا للشركة.")
                expected = (self.opening_foreign_amount * self.opening_exchange_rate).quantize(Decimal("0.0001"))
                if expected != self.opening_balance:
                    raise ValidationError("القيمة الأساسية الافتتاحية لا تطابق المبلغ الأجنبي مضروبًا في سعر الصرف.")
            elif is_foreign and self.opening_balance > 0:
                raise ValidationError({"opening_balance": "أدخل المبلغ الافتتاحي بالعملة الأجنبية وسعر الصرف المعتمد."})
            elif not is_foreign and any(value not in (None, Decimal("0")) for value in fx_values):
                raise ValidationError("لا تستخدم بيانات رصيد أجنبي مع حساب بالعملة الأساسية.")

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
        REVERSED = "reversed", "معكوس"

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
    reversal_journal_entry = models.OneToOneField(JournalEntry, null=True, blank=True, on_delete=models.PROTECT, related_name="treasury_transaction_reversal")
    reversed_at = models.DateTimeField(null=True, blank=True)
    reversed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="treasury_transactions_reversed")
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
            expected_base = (self.transaction_amount * self.exchange_rate).quantize(Decimal("0.0001"))
            if self.amount != expected_base:
                raise ValidationError({"amount": "القيمة بالعملة الأساسية لا تطابق المبلغ الأجنبي مضروبًا في سعر الصرف."})
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
            if old and old.status in (self.Status.POSTED, self.Status.REVERSED):
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
    adjustment_transaction = models.OneToOneField(TreasuryTransaction, null=True, blank=True, on_delete=models.PROTECT, related_name="bank_statement_adjustment")
    matched_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("line_number", "id")
        constraints = [
            models.UniqueConstraint(fields=("reconciliation", "line_number"), name="uq_bank_recon_line_no"),
            models.UniqueConstraint(fields=("reconciliation", "matched_transaction"), condition=models.Q(matched_transaction__isnull=False), name="uq_bank_recon_matched_tx"),
        ]


class BankStatementMatchAllocation(models.Model):
    """جزء من حركة دفترية مخصص لمطابقة سطر أو أكثر من كشف البنك."""
    line = models.ForeignKey(
        BankStatementLine, on_delete=models.CASCADE, related_name="allocations",
    )
    transaction = models.ForeignKey(
        TreasuryTransaction, on_delete=models.PROTECT, related_name="reconciliation_allocations",
    )
    amount = models.DecimalField(max_digits=20, decimal_places=4)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("created_at", "id")
        constraints = [
            models.UniqueConstraint(fields=("line", "transaction"), name="uq_bank_line_transaction_allocation"),
            models.CheckConstraint(condition=models.Q(amount__gt=0), name="ck_bank_match_allocation_positive"),
        ]

    def clean(self):
        super().clean()
        if self.line_id and self.transaction_id:
            if self.line.reconciliation.company_id != self.transaction.company_id:
                raise ValidationError("الحركة الدفترية وسطر كشف البنك يجب أن يتبعا الشركة نفسها.")


class TreasuryCheque(models.Model):
    """سجل دورة الشيكات الواردة والصادرة وربطها بحركة خزينة مرحلة."""

    class Direction(models.TextChoices):
        INCOMING = "incoming", "وارد"
        OUTGOING = "outgoing", "صادر"

    class Status(models.TextChoices):
        RECEIVED = "received", "مستلم"
        DEPOSITED = "deposited", "مودع بالبنك"
        UNDER_COLLECTION = "under_collection", "تحت التحصيل"
        ISSUED = "issued", "صادر"
        PRESENTED = "presented", "مقدم للصرف"
        CLEARED = "cleared", "تم التحصيل/الصرف"
        BOUNCED = "bounced", "مرتد"
        CANCELLED = "cancelled", "ملغي"

    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="treasury_cheques")
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name="treasury_cheques")
    fiscal_year = models.ForeignKey("companies.FiscalYear", on_delete=models.PROTECT, related_name="treasury_cheques")
    bank_account = models.ForeignKey(TreasuryAccount, on_delete=models.PROTECT, related_name="cheques")
    direction = models.CharField(max_length=8, choices=Direction.choices)
    cheque_number = models.CharField(max_length=80)
    counterparty = models.CharField(max_length=180)
    amount = models.DecimalField(max_digits=20, decimal_places=4)
    issue_date = models.DateField()
    due_date = models.DateField()
    status = models.CharField(max_length=20, choices=Status.choices)
    treasury_transaction = models.OneToOneField(
        TreasuryTransaction, null=True, blank=True, on_delete=models.PROTECT,
        related_name="cheque",
    )
    deposit_date = models.DateField(null=True, blank=True)
    clearing_date = models.DateField(null=True, blank=True)
    bounce_date = models.DateField(null=True, blank=True)
    notes = models.CharField(max_length=500, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.PROTECT, related_name="created_treasury_cheques")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("due_date", "id")
        constraints = [
            models.UniqueConstraint(fields=("company", "bank_account", "direction", "cheque_number"),
                                    name="uq_treasury_cheque_bank_number"),
            models.CheckConstraint(condition=models.Q(amount__gt=0), name="ck_treasury_cheque_amount_positive"),
        ]
        indexes = [
            models.Index(fields=("company", "direction", "status", "due_date"), name="tr_cheque_co_status_due_idx"),
        ]

    @property
    def company_id_for_permission(self):
        return self.company_id

    def clean(self):
        super().clean()
        if self.bank_account_id:
            if self.bank_account.company_id != self.company_id:
                raise ValidationError({"bank_account": "الحساب البنكي لا يتبع الشركة."})
            if self.bank_account.kind != TreasuryAccount.Kind.BANK or not self.bank_account.is_active:
                raise ValidationError({"bank_account": "اختر حسابًا بنكيًا نشطًا."})
            if self.bank_account.currency_code != self.company.base_currency.code:
                raise ValidationError({"bank_account": "دورة الشيكات في هذه المرحلة متاحة بالعملة الأساسية فقط."})
            if self.bank_account.branch_id and self.bank_account.branch_id != self.branch_id:
                raise ValidationError({"branch": "فرع الشيك لا يطابق فرع الحساب البنكي."})
        if self.branch_id and self.branch.company_id != self.company_id:
            raise ValidationError({"branch": "الفرع لا يتبع الشركة."})
        if self.fiscal_year_id and self.fiscal_year.company_id != self.company_id:
            raise ValidationError({"fiscal_year": "السنة المالية لا تتبع الشركة."})
        if self.amount <= 0:
            raise ValidationError({"amount": "قيمة الشيك يجب أن تكون أكبر من صفر."})
        if self.due_date < self.issue_date:
            raise ValidationError({"due_date": "تاريخ الاستحقاق لا يجوز أن يسبق تاريخ إصدار/استلام الشيك."})
        if self.treasury_transaction_id:
            doc = self.treasury_transaction
            expected_kind = TreasuryTransaction.Kind.RECEIPT if self.direction == self.Direction.INCOMING else TreasuryTransaction.Kind.PAYMENT
            if doc.company_id != self.company_id or doc.source_account_id != self.bank_account_id:
                raise ValidationError({"treasury_transaction": "حركة الخزينة لا تطابق الشركة أو الحساب البنكي للشيك."})
            if doc.kind != expected_kind or doc.amount != self.amount:
                raise ValidationError({"treasury_transaction": "نوع أو مبلغ حركة الخزينة لا يطابق الشيك."})


class CashCommitment(models.Model):
    """التزام نقدي خارج الذمم التشغيلية، يظهر في التوقعات ولا ينشئ قيدًا محاسبيًا."""

    class Kind(models.TextChoices):
        TAX = "tax", "ضرائب ورسوم"
        RECURRING_EXPENSE = "recurring_expense", "مصروف دوري"
        FRIDAY_OVERTIME = "friday_overtime", "أجور إضافية"
        OTHER = "other", "التزامات أخرى"

    class Status(models.TextChoices):
        OPEN = "open", "مفتوح"
        PARTIALLY_PAID = "partially_paid", "مسدد جزئيًا"
        PAID = "paid", "مسدد"
        VOID = "void", "ملغي"

    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="cash_commitments")
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name="cash_commitments", null=True, blank=True)
    commitment_type = models.CharField(max_length=24, choices=Kind.choices)
    title = models.CharField(max_length=180)
    period_label = models.CharField(max_length=40, blank=True)
    amount = models.DecimalField(max_digits=20, decimal_places=4)
    currency_code = models.CharField(max_length=3)
    exchange_rate = models.DecimalField(max_digits=20, decimal_places=10, default=1)
    base_amount = models.DecimalField(max_digits=20, decimal_places=4)
    due_date = models.DateField()
    payable_account = models.ForeignKey(ChartOfAccount, on_delete=models.PROTECT, related_name="cash_commitments")
    origin_type = models.CharField(max_length=80, blank=True)
    origin_id = models.PositiveBigIntegerField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.OPEN)
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="created_cash_commitments")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("due_date", "id")
        constraints = [
            models.CheckConstraint(condition=models.Q(amount__gt=0), name="ck_cash_commitment_amount_positive"),
            models.CheckConstraint(condition=models.Q(base_amount__gt=0), name="ck_cash_commitment_base_positive"),
            models.CheckConstraint(condition=models.Q(exchange_rate__gt=0), name="ck_cash_commitment_rate_positive"),
            models.UniqueConstraint(fields=("company", "origin_type", "origin_id"), condition=~models.Q(origin_type=""), name="uq_cash_commitment_origin"),
        ]
        indexes = [
            models.Index(fields=("company", "status", "due_date"), name="cash_commit_co_status_due"),
            models.Index(fields=("company", "commitment_type", "due_date"), name="cash_commit_co_type_due"),
        ]

    @property
    def company_id_for_permission(self):
        return self.company_id

    def clean(self):
        super().clean()
        if self.branch_id and self.branch.company_id != self.company_id:
            raise ValidationError({"branch": "الفرع لا يتبع الشركة."})
        if self.payable_account_id:
            if self.payable_account.company_id != self.company_id:
                raise ValidationError({"payable_account": "حساب الالتزام لا يتبع الشركة."})
            if self.payable_account.is_group or not self.payable_account.is_active or self.payable_account.nature != ChartOfAccount.Nature.LIABILITY:
                raise ValidationError({"payable_account": "اختر حساب خصوم تفصيليًا ونشطًا."})
        if bool(self.origin_type) != (self.origin_id is not None):
            raise ValidationError({"origin_id": "مرجع المصدر يجب أن يحتوي النوع والمعرف معًا."})
        if self.currency_code == self.company.base_currency.code:
            if self.exchange_rate != 1 or self.base_amount != self.amount:
                raise ValidationError({"exchange_rate": "الالتزام بالعملة الأساسية يجب أن يساوي قيمته الأساسية وسعره واحدًا."})
        elif self.base_amount != (self.amount * self.exchange_rate).quantize(Decimal("0.0001")):
            raise ValidationError({"base_amount": "القيمة الأساسية لا تطابق المبلغ مضروبًا في سعر الصرف."})


class CashCommitmentPayment(models.Model):
    """ربط سداد الالتزام بقيد مرحّل موجود دون إنشاء قيد مكرر."""

    commitment = models.ForeignKey(CashCommitment, on_delete=models.PROTECT, related_name="payments")
    payment_date = models.DateField()
    amount = models.DecimalField(max_digits=20, decimal_places=4)
    base_amount = models.DecimalField(max_digits=20, decimal_places=4)
    journal_entry = models.OneToOneField(JournalEntry, on_delete=models.PROTECT, related_name="cash_commitment_payment")
    reversal_journal_entry = models.OneToOneField(JournalEntry, null=True, blank=True, on_delete=models.PROTECT, related_name="reversed_cash_commitment_payment")
    reference = models.CharField(max_length=120, blank=True)
    notes = models.CharField(max_length=500, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="created_cash_commitment_payments")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("payment_date", "id")
        constraints = [
            models.CheckConstraint(condition=models.Q(amount__gt=0), name="ck_cash_commit_pay_amount_positive"),
            models.CheckConstraint(condition=models.Q(base_amount__gt=0), name="ck_cash_commit_pay_base_positive"),
        ]

    @property
    def company_id_for_permission(self):
        return self.commitment.company_id


class CashCommitmentSchedule(models.Model):
    """جدول دوري يولد استحقاقات قابلة للتوقع؛ التوليد المتكرر آمن."""

    class Frequency(models.TextChoices):
        MONTHLY = "monthly", "شهري"
        QUARTERLY = "quarterly", "ربع سنوي"
        SEMIANNUAL = "semiannual", "نصف سنوي"
        ANNUAL = "annual", "سنوي"

    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="cash_commitment_schedules")
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name="cash_commitment_schedules", null=True, blank=True)
    title = models.CharField(max_length=180)
    amount = models.DecimalField(max_digits=20, decimal_places=4)
    currency_code = models.CharField(max_length=3)
    exchange_rate = models.DecimalField(max_digits=20, decimal_places=10, default=1)
    base_amount = models.DecimalField(max_digits=20, decimal_places=4)
    frequency = models.CharField(max_length=12, choices=Frequency.choices)
    interval_months = models.PositiveSmallIntegerField()
    anchor_day = models.PositiveSmallIntegerField()
    next_due_date = models.DateField()
    end_date = models.DateField(null=True, blank=True)
    payable_account = models.ForeignKey(ChartOfAccount, on_delete=models.PROTECT, related_name="cash_commitment_schedules")
    active = models.BooleanField(default=True)
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="created_cash_commitment_schedules")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("next_due_date", "id")
        constraints = [
            models.CheckConstraint(condition=models.Q(amount__gt=0), name="ck_cash_commit_schedule_amount_positive"),
            models.CheckConstraint(condition=models.Q(base_amount__gt=0), name="ck_cash_commit_schedule_base_positive"),
            models.CheckConstraint(condition=models.Q(exchange_rate__gt=0), name="ck_cash_commit_schedule_rate_positive"),
            models.CheckConstraint(condition=models.Q(interval_months__in=(1, 3, 6, 12)), name="ck_cash_commit_schedule_interval"),
            models.CheckConstraint(condition=models.Q(anchor_day__gte=1, anchor_day__lte=31), name="ck_cash_commit_schedule_anchor"),
        ]

    @property
    def company_id_for_permission(self):
        return self.company_id


class CashCommitmentOccurrence(models.Model):
    schedule = models.ForeignKey(CashCommitmentSchedule, on_delete=models.PROTECT, related_name="occurrences")
    due_date = models.DateField()
    commitment = models.OneToOneField(CashCommitment, null=True, blank=True, on_delete=models.PROTECT, related_name="schedule_occurrence")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=("schedule", "due_date"), name="uq_cash_commit_occurrence_date")]
        ordering = ("due_date", "id")



class PettyCashFund(models.Model):
    """Imprest fund assigned to a named custodian and represented by a dedicated cash GL account."""
    class Status(models.TextChoices):
        ACTIVE = "active", "نشطة"
        SUSPENDED = "suspended", "موقوفة"
        CLOSED = "closed", "مغلقة"

    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="petty_cash_funds")
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name="petty_cash_funds")
    code = models.CharField(max_length=32)
    name = models.CharField(max_length=160)
    custodian = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="petty_cash_funds")
    funding_account = models.ForeignKey(TreasuryAccount, on_delete=models.PROTECT, related_name="petty_cash_funding_funds")
    petty_cash_account = models.OneToOneField(TreasuryAccount, on_delete=models.PROTECT, related_name="petty_cash_fund")
    authorized_amount = models.DecimalField(max_digits=20, decimal_places=4)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.ACTIVE)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="created_petty_cash_funds")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("code",)
        constraints = [
            models.UniqueConstraint(fields=("company", "code"), name="uq_petty_cash_fund_company_code"),
            models.CheckConstraint(condition=models.Q(authorized_amount__gt=0), name="ck_petty_cash_fund_authorized_positive"),
        ]

    @property
    def company_id_for_permission(self):
        return self.company_id

    def clean(self):
        super().clean()
        if self.branch_id and self.branch.company_id != self.company_id:
            raise ValidationError({"branch": "فرع العهدة لا يتبع الشركة."})
        for field_name in ("funding_account", "petty_cash_account"):
            account = getattr(self, field_name, None)
            if account and account.company_id != self.company_id:
                raise ValidationError({field_name: "حساب الخزينة لا يتبع الشركة."})
            if account and account.branch_id not in (None, self.branch_id):
                raise ValidationError({field_name: "حساب الخزينة لا يتبع فرع العهدة."})
            if account and account.currency_code != self.company.base_currency.code:
                raise ValidationError({field_name: "العهدة تعمل بالعملة الأساسية للشركة حاليًا."})
        if self.funding_account_id and self.petty_cash_account_id:
            if self.funding_account_id == self.petty_cash_account_id:
                raise ValidationError("حساب تمويل العهدة يجب أن يختلف عن حساب العهدة.")
            if self.petty_cash_account.kind != TreasuryAccount.Kind.CASH:
                raise ValidationError({"petty_cash_account": "اربط العهدة بحساب خزينة من نوع صندوق."})
            if self.funding_account.kind not in (TreasuryAccount.Kind.CASH, TreasuryAccount.Kind.BANK):
                raise ValidationError({"funding_account": "اختر حساب خزينة أو بنك فعال."})
        if self.petty_cash_account_id and self.petty_cash_account.account.nature != ChartOfAccount.Nature.ASSET:
            raise ValidationError({"petty_cash_account": "حساب العهدة يجب أن يكون من طبيعة الأصول."})


class PettyCashSettlement(models.Model):
    """One approved expense settlement posted as a single balanced journal."""
    fund = models.ForeignKey(PettyCashFund, on_delete=models.PROTECT, related_name="settlements")
    settlement_date = models.DateField()
    journal_entry = models.OneToOneField(JournalEntry, on_delete=models.PROTECT, related_name="petty_cash_settlement")
    total_amount = models.DecimalField(max_digits=20, decimal_places=4)
    replenished_amount = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="petty_cash_settlements")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-settlement_date", "-id")
        constraints = [
            models.CheckConstraint(condition=models.Q(total_amount__gt=0), name="ck_petty_cash_settlement_total_positive"),
            models.CheckConstraint(condition=models.Q(replenished_amount__gte=0), name="ck_petty_cash_settlement_replenished_nonnegative"),
        ]

    @property
    def company_id_for_permission(self):
        return self.fund.company_id


class PettyCashExpense(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "مسودة"
        PENDING_APPROVAL = "pending_approval", "بانتظار الاعتماد"
        APPROVED = "approved", "معتمد"
        REJECTED = "rejected", "مرفوض"
        SETTLED = "settled", "تمت التسوية"
        VOID = "void", "ملغاة"

    fund = models.ForeignKey(PettyCashFund, on_delete=models.PROTECT, related_name="expenses")
    expense_date = models.DateField()
    description = models.CharField(max_length=300)
    amount = models.DecimalField(max_digits=20, decimal_places=4)
    expense_account = models.ForeignKey(ChartOfAccount, on_delete=models.PROTECT, related_name="petty_cash_expenses")
    receipt_number = models.CharField(max_length=100, blank=True)
    receipt_file_data = models.BinaryField(null=True, blank=True, editable=False)
    receipt_file_name = models.CharField(max_length=255, blank=True)
    receipt_file_content_type = models.CharField(max_length=100, blank=True)
    vendor_name = models.CharField(max_length=180, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    submitted_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="approved_petty_cash_expenses")
    approved_at = models.DateTimeField(null=True, blank=True)
    approval_note = models.CharField(max_length=500, blank=True)
    rejected_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="rejected_petty_cash_expenses")
    rejected_at = models.DateTimeField(null=True, blank=True)
    rejection_note = models.CharField(max_length=500, blank=True)
    settlement = models.ForeignKey(PettyCashSettlement, null=True, blank=True, on_delete=models.PROTECT, related_name="expenses")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="petty_cash_expenses")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("expense_date", "id")
        constraints = [
            models.CheckConstraint(condition=models.Q(amount__gt=0), name="ck_petty_cash_expense_amount_positive"),
        ]
        indexes = [models.Index(fields=("fund", "status", "expense_date"), name="petty_cash_exp_status_idx")]

    @property
    def company_id_for_permission(self):
        return self.fund.company_id

    def clean(self):
        super().clean()
        if self.expense_account_id:
            if self.expense_account.company_id != self.fund.company_id:
                raise ValidationError({"expense_account": "حساب المصروف لا يتبع شركة العهدة."})
            if self.expense_account.is_group or not self.expense_account.is_active or not self.expense_account.allow_posting:
                raise ValidationError({"expense_account": "اختر حساب مصروف تفصيليًا ونشطًا وقابلًا للترحيل."})
            if self.expense_account.nature != ChartOfAccount.Nature.EXPENSE:
                raise ValidationError({"expense_account": "الحساب المقابل يجب أن يكون من طبيعة المصروفات."})
        if self.settlement_id and self.settlement.fund_id != self.fund_id:
            raise ValidationError({"settlement": "التسوية لا تتبع نفس العهدة."})


class PettyCashTransfer(models.Model):
    class Kind(models.TextChoices):
        INITIAL = "initial", "صرف عهدة"
        REPLENISHMENT = "replenishment", "تغذية عهدة"
        CLOSURE_RETURN = "closure_return", "رد المتبقي عند الإغلاق"

    fund = models.ForeignKey(PettyCashFund, on_delete=models.PROTECT, related_name="transfers")
    kind = models.CharField(max_length=16, choices=Kind.choices)
    amount = models.DecimalField(max_digits=20, decimal_places=4)
    treasury_transaction = models.OneToOneField(TreasuryTransaction, on_delete=models.PROTECT, related_name="petty_cash_transfer")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="petty_cash_transfers")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("created_at", "id")
        constraints = [models.CheckConstraint(condition=models.Q(amount__gt=0), name="ck_petty_cash_transfer_amount_positive")]

    @property
    def company_id_for_permission(self):
        return self.fund.company_id


class PettyCashCount(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "بانتظار الاعتماد"
        APPROVED = "approved", "معتمد"
        REJECTED = "rejected", "مرفوض"

    fund = models.ForeignKey(PettyCashFund, on_delete=models.PROTECT, related_name="counts")
    count_date = models.DateField()
    expected_balance = models.DecimalField(max_digits=20, decimal_places=4)
    actual_balance = models.DecimalField(max_digits=20, decimal_places=4)
    notes = models.CharField(max_length=500, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    counted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="petty_cash_counts")
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="reviewed_petty_cash_counts")
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_note = models.CharField(max_length=500, blank=True)
    adjustment_journal_entry = models.OneToOneField(JournalEntry, null=True, blank=True, on_delete=models.PROTECT, related_name="petty_cash_count_adjustment")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-count_date", "-id")
        constraints = [
            models.CheckConstraint(condition=models.Q(expected_balance__gte=0), name="ck_petty_cash_count_expected_nonnegative"),
            models.CheckConstraint(condition=models.Q(actual_balance__gte=0), name="ck_petty_cash_count_actual_nonnegative"),
        ]

    @property
    def variance(self):
        return self.actual_balance - self.expected_balance

    @property
    def company_id_for_permission(self):
        return self.fund.company_id


class PettyCashCustodianHandover(models.Model):
    fund = models.ForeignKey(PettyCashFund, on_delete=models.PROTECT, related_name="custodian_handovers")
    previous_custodian = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="petty_cash_handovers_from")
    new_custodian = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="petty_cash_handovers_to")
    handover_date = models.DateField()
    expected_balance = models.DecimalField(max_digits=20, decimal_places=4)
    actual_balance = models.DecimalField(max_digits=20, decimal_places=4)
    notes = models.CharField(max_length=500, blank=True)
    handed_over_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="petty_cash_handovers_recorded")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-handover_date", "-id")

    @property
    def company_id_for_permission(self):
        return self.fund.company_id
