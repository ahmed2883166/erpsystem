from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, MaxValueValidator
from django.db import models
from django.utils import timezone

from companies.models import Company


class DiscountApprovalPolicy(models.Model):
    company = models.OneToOneField(Company, on_delete=models.PROTECT, related_name="discount_approval_policy")
    max_discount_percent = models.DecimalField(
        max_digits=7, decimal_places=4, null=True, blank=True,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        help_text="الحد الأقصى للخصم المسموح دون موافقة مدير الشركة؛ اتركه فارغًا لتعطيله.",
    )
    max_sales_invoice_amount = models.DecimalField(
        max_digits=20, decimal_places=4, null=True, blank=True,
        validators=[MinValueValidator(0)],
        help_text="أقصى إجمالي فاتورة مبيعات دون موافقة مدير الشركة، بعملة الشركة الأساسية.",
    )
    max_purchase_invoice_amount = models.DecimalField(
        max_digits=20, decimal_places=4, null=True, blank=True,
        validators=[MinValueValidator(0)],
        help_text="أقصى إجمالي فاتورة مشتريات دون موافقة مدير الشركة.",
    )
    max_payment_amount = models.DecimalField(
        max_digits=20, decimal_places=4, null=True, blank=True,
        validators=[MinValueValidator(0)],
        help_text="أقصى قيمة سند صرف دون موافقة مدير الشركة.",
    )
    created_by = models.ForeignKey("accounts.User", on_delete=models.PROTECT, related_name="created_discount_policies")
    updated_by = models.ForeignKey("accounts.User", on_delete=models.PROTECT, related_name="updated_discount_policies")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "سياسة حدود الموافقات"
        verbose_name_plural = "سياسات حدود الموافقات"
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(max_discount_percent__isnull=False)
                    | models.Q(max_sales_invoice_amount__isnull=False)
                    | models.Q(max_purchase_invoice_amount__isnull=False)
                    | models.Q(max_payment_amount__isnull=False)
                ),
                name="discount_policy_has_limit",
            ),
        ]

    def clean(self):
        super().clean()
        if self.max_discount_percent is not None and not (0 <= self.max_discount_percent <= 100):
            raise ValidationError({"max_discount_percent": "نسبة الحد يجب أن تكون بين 0 و100."})
        if not any(value is not None for value in (
            self.max_discount_percent, self.max_sales_invoice_amount,
            self.max_purchase_invoice_amount, self.max_payment_amount,
        )):
            raise ValidationError("يجب تحديد حد مالي واحد على الأقل.")


class SalesInvoiceApproval(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "بانتظار الموافقة"
        APPROVED = "approved", "تمت الموافقة"
        REJECTED = "rejected", "مرفوض"
        SUPERSEDED = "superseded", "تم استبداله بطلب أحدث"

    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="sales_invoice_approvals")
    invoice = models.ForeignKey("sales.SalesInvoice", on_delete=models.PROTECT, related_name="approval_requests")
    requested_by = models.ForeignKey("accounts.User", on_delete=models.PROTECT, related_name="sales_discount_requests")
    requested_limit = models.DecimalField(max_digits=7, decimal_places=4, null=True, blank=True)
    requested_invoice_limit = models.DecimalField(max_digits=20, decimal_places=4, null=True, blank=True)
    requested_discount_total = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    requested_max_discount_percent = models.DecimalField(max_digits=7, decimal_places=4, default=0)
    requested_lines_snapshot = models.JSONField(default=list, editable=False)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    decided_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.PROTECT,
        related_name="decided_sales_discount_requests",
    )
    decision_note = models.CharField(max_length=500, blank=True)
    requested_at = models.DateTimeField(auto_now_add=True)
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("status", "-requested_at")
        indexes = [
            models.Index(fields=("company", "status", "requested_at"), name="approval_co_status_date_idx"),
        ]
        verbose_name = "طلب موافقة خصم"
        verbose_name_plural = "طلبات موافقات الخصم"

    def clean(self):
        super().clean()
        if self.invoice_id and self.invoice.company_id != self.company_id:
            raise ValidationError({"invoice": "الفاتورة لا تتبع الشركة المحددة."})

    def save(self, *args, **kwargs):
        if self.pk:
            old = type(self).objects.get(pk=self.pk)
            if old.status != self.Status.PENDING:
                raise ValidationError("قرار الموافقة نهائي ولا يمكن تغييره.")
            immutable_fields = (
                "company_id", "invoice_id", "requested_by_id", "requested_limit",
                "requested_invoice_limit", "requested_discount_total", "requested_max_discount_percent",
                "requested_lines_snapshot", "requested_at",
            )
            if any(getattr(old, field) != getattr(self, field) for field in immutable_fields):
                raise ValidationError("بيانات ولقطة طلب الموافقة لا يمكن تعديلها.")
            if self.status not in (self.Status.APPROVED, self.Status.REJECTED, self.Status.SUPERSEDED):
                raise ValidationError("حالة قرار الموافقة غير صالحة.")
            if self.status == self.Status.SUPERSEDED and self.decided_by_id:
                raise ValidationError("الطلب المستبدل لا يحمل قرار مدير.")
            if self.status in (self.Status.APPROVED, self.Status.REJECTED) and not self.decided_by_id:
                raise ValidationError("قرار المدير مطلوب.")
            if self.decided_at is None:
                self.decided_at = timezone.now()
        elif self.status != self.Status.PENDING:
            raise ValidationError("يجب إنشاء طلب الموافقة بحالة انتظار.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("لا يمكن حذف سجل طلب الموافقة.")


class FinancialApprovalRequest(models.Model):
    class DocumentType(models.TextChoices):
        PURCHASE_INVOICE = "purchase_invoice", "فاتورة مشتريات"
        SUPPLIER_PAYMENT = "supplier_payment", "سند صرف مورد"
        TREASURY_PAYMENT = "treasury_payment", "سند صرف خزينة"

    class Status(models.TextChoices):
        PENDING = "pending", "بانتظار الموافقة"
        APPROVED = "approved", "تمت الموافقة"
        REJECTED = "rejected", "مرفوض"
        SUPERSEDED = "superseded", "تم استبداله بطلب أحدث"

    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="financial_approval_requests")
    document_type = models.CharField(max_length=24, choices=DocumentType.choices)
    purchase_invoice = models.ForeignKey("purchasing.PurchaseInvoice", null=True, blank=True, on_delete=models.PROTECT, related_name="approval_requests")
    supplier_payment = models.ForeignKey("purchasing.SupplierPayment", null=True, blank=True, on_delete=models.PROTECT, related_name="approval_requests")
    treasury_transaction = models.ForeignKey("treasury.TreasuryTransaction", null=True, blank=True, on_delete=models.PROTECT, related_name="approval_requests")
    requested_by = models.ForeignKey("accounts.User", on_delete=models.PROTECT, related_name="created_financial_approval_requests")
    requested_limit = models.DecimalField(max_digits=20, decimal_places=4)
    requested_amount = models.DecimalField(max_digits=20, decimal_places=4)
    requested_snapshot = models.JSONField(default=dict, editable=False)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    decided_by = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="decided_financial_approval_requests")
    decision_note = models.CharField(max_length=500, blank=True)
    requested_at = models.DateTimeField(auto_now_add=True)
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("status", "-requested_at")
        indexes = [models.Index(fields=("company", "status", "requested_at"), name="fin_approval_co_status_idx")]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(document_type="purchase_invoice", purchase_invoice__isnull=False, supplier_payment__isnull=True, treasury_transaction__isnull=True)
                    | models.Q(document_type="supplier_payment", purchase_invoice__isnull=True, supplier_payment__isnull=False, treasury_transaction__isnull=True)
                    | models.Q(document_type="treasury_payment", purchase_invoice__isnull=True, supplier_payment__isnull=True, treasury_transaction__isnull=False)
                ),
                name="financial_approval_one_document",
            ),
        ]

    def save(self, *args, **kwargs):
        if self.pk:
            old = type(self).objects.get(pk=self.pk)
            if old.status != self.Status.PENDING:
                raise ValidationError("قرار الموافقة نهائي ولا يمكن تغييره.")
            immutable = ("company_id", "document_type", "purchase_invoice_id", "supplier_payment_id",
                         "treasury_transaction_id", "requested_by_id", "requested_limit",
                         "requested_amount", "requested_snapshot", "requested_at")
            if any(getattr(old, field) != getattr(self, field) for field in immutable):
                raise ValidationError("بيانات ولقطة طلب الموافقة لا يمكن تعديلها.")
            if self.status not in (self.Status.APPROVED, self.Status.REJECTED, self.Status.SUPERSEDED):
                raise ValidationError("حالة قرار الموافقة غير صالحة.")
            if self.status == self.Status.SUPERSEDED and self.decided_by_id:
                raise ValidationError("الطلب المستبدل لا يحمل قرار مدير.")
            if self.status in (self.Status.APPROVED, self.Status.REJECTED) and not self.decided_by_id:
                raise ValidationError("قرار المدير مطلوب.")
            if self.decided_at is None:
                self.decided_at = timezone.now()
        elif self.status != self.Status.PENDING:
            raise ValidationError("يجب إنشاء طلب الموافقة بحالة انتظار.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("لا يمكن حذف سجل طلب الموافقة.")
