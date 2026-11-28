"""نماذج سياسات الاعتماد وحدود الخصم والموافقات المالية على المستندات."""

from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, MaxValueValidator
from django.db import models
from django.utils import timezone

from companies.models import Company


class DiscountApprovalPolicy(models.Model):
    """تحدد حدود الخصم وقيمة الفاتورة التي تتطلب اعتمادًا إداريًا."""
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
    max_purchase_order_amount = models.DecimalField(
        max_digits=20, decimal_places=4, null=True, blank=True,
        validators=[MinValueValidator(0)],
        help_text="أقصى إجمالي أمر شراء دون موافقة مدير الشركة.",
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
                    | models.Q(max_purchase_order_amount__isnull=False)
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
            self.max_purchase_invoice_amount, self.max_purchase_order_amount, self.max_payment_amount,
        )):
            raise ValidationError("يجب تحديد حد مالي واحد على الأقل.")


class SalesInvoiceApproval(models.Model):
    """يحفظ طلب اعتماد خصم أو فاتورة بيع مع لقطة بياناتها وقت الطلب."""
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
    requested_credit_limit = models.DecimalField(max_digits=20, decimal_places=4, null=True, blank=True)
    requested_credit_exposure = models.DecimalField(max_digits=20, decimal_places=4, null=True, blank=True)
    requested_credit_amount = models.DecimalField(max_digits=20, decimal_places=4, null=True, blank=True)
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
                "requested_credit_limit", "requested_credit_exposure", "requested_credit_amount",
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
    """يمثل طلب اعتماد مالي مرتبطًا بمستند حساس قبل ترحيله."""
    class DocumentType(models.TextChoices):
        PURCHASE_INVOICE = "purchase_invoice", "فاتورة مشتريات"
        PURCHASE_ORDER = "purchase_order", "أمر شراء"
        SUPPLIER_PAYMENT = "supplier_payment", "سند صرف مورد"
        TREASURY_PAYMENT = "treasury_payment", "سند صرف خزينة"
        ASSET_PURCHASE_INVOICE = "asset_purchase_invoice", "فاتورة شراء أصل ثابت"
        ASSET_SUPPLIER_PAYMENT = "asset_supplier_payment", "سند سداد أصل ثابت"

    class Status(models.TextChoices):
        PENDING = "pending", "بانتظار الموافقة"
        APPROVED = "approved", "تمت الموافقة"
        REJECTED = "rejected", "مرفوض"
        SUPERSEDED = "superseded", "تم استبداله بطلب أحدث"

    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="financial_approval_requests")
    document_type = models.CharField(max_length=24, choices=DocumentType.choices)
    purchase_invoice = models.ForeignKey("purchasing.PurchaseInvoice", null=True, blank=True, on_delete=models.PROTECT, related_name="approval_requests")
    purchase_order = models.ForeignKey("purchasing.PurchaseOrder", null=True, blank=True, on_delete=models.PROTECT, related_name="approval_requests")
    supplier_payment = models.ForeignKey("purchasing.SupplierPayment", null=True, blank=True, on_delete=models.PROTECT, related_name="approval_requests")
    treasury_transaction = models.ForeignKey("treasury.TreasuryTransaction", null=True, blank=True, on_delete=models.PROTECT, related_name="approval_requests")
    asset_purchase_invoice = models.ForeignKey("assets.AssetPurchaseInvoice", null=True, blank=True, on_delete=models.PROTECT, related_name="approval_requests")
    asset_supplier_payment = models.ForeignKey("assets.AssetSupplierPayment", null=True, blank=True, on_delete=models.PROTECT, related_name="approval_requests")
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
                    models.Q(document_type="purchase_invoice", purchase_invoice__isnull=False, purchase_order__isnull=True, supplier_payment__isnull=True, treasury_transaction__isnull=True, asset_purchase_invoice__isnull=True, asset_supplier_payment__isnull=True)
                    | models.Q(document_type="purchase_order", purchase_invoice__isnull=True, purchase_order__isnull=False, supplier_payment__isnull=True, treasury_transaction__isnull=True, asset_purchase_invoice__isnull=True, asset_supplier_payment__isnull=True)
                    | models.Q(document_type="supplier_payment", purchase_invoice__isnull=True, purchase_order__isnull=True, supplier_payment__isnull=False, treasury_transaction__isnull=True, asset_purchase_invoice__isnull=True, asset_supplier_payment__isnull=True)
                    | models.Q(document_type="treasury_payment", purchase_invoice__isnull=True, purchase_order__isnull=True, supplier_payment__isnull=True, treasury_transaction__isnull=False, asset_purchase_invoice__isnull=True, asset_supplier_payment__isnull=True)
                    | models.Q(document_type="asset_purchase_invoice", purchase_invoice__isnull=True, purchase_order__isnull=True, supplier_payment__isnull=True, treasury_transaction__isnull=True, asset_purchase_invoice__isnull=False, asset_supplier_payment__isnull=True)
                    | models.Q(document_type="asset_supplier_payment", purchase_invoice__isnull=True, purchase_order__isnull=True, supplier_payment__isnull=True, treasury_transaction__isnull=True, asset_purchase_invoice__isnull=True, asset_supplier_payment__isnull=False)
                ),
                name="financial_approval_one_document",
            ),
        ]

    def save(self, *args, **kwargs):
        relations = {
            self.DocumentType.PURCHASE_INVOICE: ("purchase_invoice", self.purchase_invoice_id),
            self.DocumentType.PURCHASE_ORDER: ("purchase_order", self.purchase_order_id),
            self.DocumentType.SUPPLIER_PAYMENT: ("supplier_payment", self.supplier_payment_id),
            self.DocumentType.TREASURY_PAYMENT: ("treasury_transaction", self.treasury_transaction_id),
            self.DocumentType.ASSET_PURCHASE_INVOICE: ("asset_purchase_invoice", self.asset_purchase_invoice_id),
            self.DocumentType.ASSET_SUPPLIER_PAYMENT: ("asset_supplier_payment", self.asset_supplier_payment_id),
        }
        selected = relations.get(self.document_type)
        if not selected or selected[1] is None:
            raise ValidationError("نوع المستند ورابطه غير متطابقين.")
        expected_relation = selected[0]
        if any(
            getattr(self, f"{field}_id") is not None
            for field in ("purchase_invoice", "purchase_order", "supplier_payment", "treasury_transaction", "asset_purchase_invoice", "asset_supplier_payment")
            if field != expected_relation
        ):
            raise ValidationError("طلب الموافقة يجب أن يرتبط بمستند واحد فقط.")
        document = getattr(self, expected_relation)
        if document.company_id != self.company_id:
            raise ValidationError("المستند لا يتبع الشركة المحددة في طلب الموافقة.")
        if self.pk:
            old = type(self).objects.get(pk=self.pk)
            if old.status != self.Status.PENDING:
                raise ValidationError("قرار الموافقة نهائي ولا يمكن تغييره.")
            immutable = ("company_id", "document_type", "purchase_invoice_id", "purchase_order_id", "supplier_payment_id",
                         "treasury_transaction_id", "asset_purchase_invoice_id", "asset_supplier_payment_id", "requested_by_id", "requested_limit",
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
