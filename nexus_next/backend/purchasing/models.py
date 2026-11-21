from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, MaxValueValidator
from django.db import models

from companies.models import Company
from inventory.models import StockMovement, StockMovementLine, Warehouse
from suppliers.models import Supplier


class PurchaseReceipt(models.Model):
    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="purchase_receipts")
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name="purchase_receipts")
    warehouse = models.ForeignKey(Warehouse, on_delete=models.PROTECT, related_name="purchase_receipts")
    stock_movement = models.OneToOneField(StockMovement, on_delete=models.PROTECT, related_name="purchase_receipt")
    receipt_number = models.CharField(max_length=40)
    receipt_date = models.DateField()
    supplier_delivery_ref = models.CharField(max_length=100, blank=True)
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="created_purchase_receipts")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-receipt_date", "-id")
        constraints = [
            models.UniqueConstraint(fields=("company", "receipt_number"), name="uq_purchase_receipt_company_no"),
        ]
        indexes = [models.Index(fields=("company", "receipt_date"), name="pur_receipt_co_date_idx")]

    @property
    def company_id_for_permission(self):
        return self.company_id

    @property
    def status(self):
        return self.stock_movement.status

    def clean(self):
        super().clean()
        if self.supplier_id and self.supplier.company_id != self.company_id:
            raise ValidationError({"supplier": "المورد لا يتبع الشركة المحددة."})
        if self.warehouse_id and self.warehouse.company_id != self.company_id:
            raise ValidationError({"warehouse": "المخزن لا يتبع الشركة المحددة."})
        if self.stock_movement_id and (
            self.stock_movement.company_id != self.company_id
            or self.stock_movement.destination_warehouse_id != self.warehouse_id
            or self.stock_movement.movement_type != StockMovement.MovementType.RECEIPT
        ):
            raise ValidationError({"stock_movement": "حركة المخزون لا تطابق إذن الاستلام."})

    def __str__(self):
        return self.receipt_number


class PurchaseInvoice(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "مسودة"
        POSTED = "posted", "مرحّلة"

    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="purchase_invoices")
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name="purchase_invoices")
    receipt = models.OneToOneField(PurchaseReceipt, on_delete=models.PROTECT, related_name="invoice")
    invoice_number = models.CharField(max_length=80)
    invoice_date = models.DateField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    net_total = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    tax_total = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    grand_total = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    notes = models.TextField(blank=True)
    journal_entry = models.OneToOneField("accounting.JournalEntry", null=True, blank=True, on_delete=models.PROTECT, related_name="purchase_invoice")
    created_by = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="created_purchase_invoices")
    created_at = models.DateTimeField(auto_now_add=True)
    posted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-invoice_date", "-id")
        constraints = [
            models.UniqueConstraint(fields=("company", "invoice_number"), name="uq_purchase_invoice_company_no"),
        ]
        indexes = [models.Index(fields=("company", "status", "invoice_date"), name="pur_invoice_co_status_date_idx")]

    @property
    def company_id_for_permission(self):
        return self.company_id

    def clean(self):
        super().clean()
        if self.supplier_id and self.supplier.company_id != self.company_id:
            raise ValidationError({"supplier": "المورد لا يتبع الشركة المحددة."})
        if self.receipt_id and (
            self.receipt.company_id != self.company_id or self.receipt.supplier_id != self.supplier_id
        ):
            raise ValidationError({"receipt": "إذن الاستلام لا يتبع المورد والشركة المحددين."})
        if self.status == self.Status.POSTED and self.pk:
            old = type(self).objects.filter(pk=self.pk).values_list("status", flat=True).first()
            if old == self.Status.POSTED:
                raise ValidationError("فاتورة المورد المرحلة غير قابلة للتعديل.")

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk, status=self.Status.POSTED).exists():
            raise ValidationError("فاتورة المورد المرحلة غير قابلة للتعديل.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.status == self.Status.POSTED:
            raise ValidationError("لا يمكن حذف فاتورة مورد مرحلة.")
        return super().delete(*args, **kwargs)

    def __str__(self):
        return self.invoice_number


class PurchaseInvoiceLine(models.Model):
    invoice = models.ForeignKey(PurchaseInvoice, on_delete=models.PROTECT, related_name="lines")
    receipt_line = models.OneToOneField(StockMovementLine, on_delete=models.PROTECT, related_name="purchase_invoice_line")
    unit_price = models.DecimalField(max_digits=18, decimal_places=4, validators=[MinValueValidator(0)])
    tax_rate = models.DecimalField(max_digits=7, decimal_places=4, default=0, validators=[MinValueValidator(0), MaxValueValidator(100)])
    net_amount = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    tax_amount = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    total_amount = models.DecimalField(max_digits=20, decimal_places=4, default=0)

    class Meta:
        ordering = ("id",)

    def calculate(self):
        quantity = self.receipt_line.quantity
        self.net_amount = (quantity * self.unit_price).quantize(Decimal("0.0001"))
        self.tax_amount = (self.net_amount * self.tax_rate / Decimal("100")).quantize(Decimal("0.0001"))
        self.total_amount = self.net_amount + self.tax_amount

    def clean(self):
        super().clean()
        if self.invoice_id and self.receipt_line_id:
            receipt = self.invoice.receipt
            if self.receipt_line.movement_id != receipt.stock_movement_id:
                raise ValidationError({"receipt_line": "سطر الصنف لا يتبع إذن الاستلام المحدد."})
            if self.receipt_line.product.product_type != "goods":
                raise ValidationError({"receipt_line": "لا يمكن فوترة خدمة من خلال استلام مخزني."})

    def save(self, *args, **kwargs):
        if self.invoice_id and PurchaseInvoice.objects.filter(
            pk=self.invoice_id, status=PurchaseInvoice.Status.POSTED
        ).exists():
            raise ValidationError("لا يمكن تعديل سطور فاتورة مورد مرحلة.")
        self.calculate()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.invoice.status == PurchaseInvoice.Status.POSTED:
            raise ValidationError("لا يمكن حذف سطور فاتورة مورد مرحلة.")
        return super().delete(*args, **kwargs)
