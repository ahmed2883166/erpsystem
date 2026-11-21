from django.core.exceptions import ValidationError
from django.db import models

from companies.models import Company, Branch
from accounting.models import ChartOfAccount, JournalEntry


class TreasuryAccount(models.Model):
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
    currency_code = models.CharField(max_length=3, default="EGP")
    opening_balance = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("kind", "code")
        constraints = [models.UniqueConstraint(fields=("company", "code"), name="uq_treasury_account_company_code")]
        indexes = [models.Index(fields=("company", "kind", "is_active"))]

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

    def __str__(self):
        return f"{self.code} - {self.name_ar}"


class TreasuryTransaction(models.Model):
    class Kind(models.TextChoices):
        RECEIPT = "receipt", "سند قبض"
        PAYMENT = "payment", "سند صرف"
        TRANSFER = "transfer", "تحويل بين الخزائن"

    class Status(models.TextChoices):
        DRAFT = "draft", "مسودة"
        POSTED = "posted", "مرحّل"

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
    amount = models.DecimalField(max_digits=20, decimal_places=4)
    description = models.CharField(max_length=500)
    reference = models.CharField(max_length=120, blank=True)
    journal_entry = models.OneToOneField(JournalEntry, null=True, blank=True, on_delete=models.PROTECT, related_name="treasury_transaction")
    created_by = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="created_treasury_transactions")
    created_at = models.DateTimeField(auto_now_add=True)
    posted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-transaction_date", "-id")
        constraints = [models.UniqueConstraint(fields=("company", "document_number"), name="uq_treasury_doc_company_no")]
        indexes = [models.Index(fields=("company", "status", "transaction_date"))]

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
        if self.pk and type(self).objects.filter(pk=self.pk, status=self.Status.POSTED).exists():
            raise ValidationError("المستند المرحّل غير قابل للتعديل.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.status != self.Status.DRAFT:
            raise ValidationError("لا يمكن حذف مستند خزينة مرحّل.")
        return super().delete(*args, **kwargs)
