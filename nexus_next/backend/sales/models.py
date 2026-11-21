from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, MaxValueValidator
from django.db import models

from accounting.models import TaxCode
from companies.models import Company
from customers.models import Customer
from inventory.models import StockMovement, StockMovementLine, Warehouse


class SalesDelivery(models.Model):
    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="sales_deliveries")
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="sales_deliveries")
    warehouse = models.ForeignKey(Warehouse, on_delete=models.PROTECT, related_name="sales_deliveries")
    stock_movement = models.OneToOneField(StockMovement, on_delete=models.PROTECT, related_name="sales_delivery")
    delivery_number = models.CharField(max_length=40)
    delivery_date = models.DateField()
    customer_reference = models.CharField(max_length=100, blank=True)
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="created_sales_deliveries")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-delivery_date", "-id")
        constraints = [models.UniqueConstraint(fields=("company", "delivery_number"), name="uq_sales_delivery_company_no")]
        indexes = [models.Index(fields=("company", "delivery_date"), name="sales_delivery_co_date_idx")]

    @property
    def company_id_for_permission(self):
        return self.company_id

    @property
    def status(self):
        return self.stock_movement.status

    def clean(self):
        super().clean()
        if self.customer_id and self.customer.company_id != self.company_id:
            raise ValidationError({"customer": "العميل لا يتبع الشركة المحددة."})
        if self.warehouse_id and self.warehouse.company_id != self.company_id:
            raise ValidationError({"warehouse": "المخزن لا يتبع الشركة المحددة."})
        if self.stock_movement_id and (
            self.stock_movement.company_id != self.company_id
            or self.stock_movement.source_warehouse_id != self.warehouse_id
            or self.stock_movement.movement_type != StockMovement.MovementType.ISSUE
        ):
            raise ValidationError({"stock_movement": "حركة الصرف لا تطابق إذن التسليم."})


class SalesInvoice(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "مسودة"
        POSTED = "posted", "مرحّلة"

    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="sales_invoices")
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="sales_invoices")
    delivery = models.OneToOneField(SalesDelivery, on_delete=models.PROTECT, related_name="invoice")
    invoice_number = models.CharField(max_length=80)
    invoice_date = models.DateField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    net_total = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    tax_total = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    grand_total = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    notes = models.TextField(blank=True)
    journal_entry = models.OneToOneField("accounting.JournalEntry", null=True, blank=True, on_delete=models.PROTECT, related_name="sales_invoice")
    created_by = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="created_sales_invoices")
    created_at = models.DateTimeField(auto_now_add=True)
    posted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-invoice_date", "-id")
        constraints = [models.UniqueConstraint(fields=("company", "invoice_number"), name="uq_sales_invoice_company_no")]
        indexes = [models.Index(fields=("company", "status", "invoice_date"), name="sales_invoice_co_status_idx")]

    @property
    def company_id_for_permission(self):
        return self.company_id

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk, status=self.Status.POSTED).exists():
            raise ValidationError("فاتورة المبيعات المرحلة غير قابلة للتعديل.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.status == self.Status.POSTED:
            raise ValidationError("لا يمكن حذف فاتورة مبيعات مرحلة.")
        return super().delete(*args, **kwargs)

    def clean(self):
        super().clean()
        if self.customer_id and self.customer.company_id != self.company_id:
            raise ValidationError({"customer": "العميل لا يتبع الشركة المحددة."})
        if self.delivery_id and (
            self.delivery.company_id != self.company_id or self.delivery.customer_id != self.customer_id
        ):
            raise ValidationError({"delivery": "إذن التسليم لا يتبع العميل والشركة المحددين."})


class SalesInvoiceLine(models.Model):
    invoice = models.ForeignKey(SalesInvoice, on_delete=models.PROTECT, related_name="lines")
    delivery_line = models.OneToOneField(StockMovementLine, on_delete=models.PROTECT, related_name="sales_invoice_line")
    unit_price = models.DecimalField(max_digits=18, decimal_places=4, validators=[MinValueValidator(0)])
    tax_rate = models.DecimalField(max_digits=7, decimal_places=4, default=0, validators=[MinValueValidator(0), MaxValueValidator(100)])
    tax_code = models.ForeignKey(TaxCode, null=True, blank=True, on_delete=models.PROTECT, related_name="sales_invoice_lines")
    net_amount = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    tax_amount = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    total_amount = models.DecimalField(max_digits=20, decimal_places=4, default=0)

    class Meta:
        ordering = ("id",)

    def calculate(self):
        if self.tax_code_id:
            code = self.tax_code
            if (not code.is_active or code.company_id != self.invoice.company_id
                    or code.country != self.invoice.company.country
                    or code.category == TaxCode.Category.REVERSE_CHARGE
                    or code.effective_from > self.invoice.invoice_date
                    or (code.effective_to and code.effective_to < self.invoice.invoice_date)):
                raise ValidationError({"tax_code": "كود الضريبة غير صالح لهذه الشركة أو تاريخ الفاتورة."})
            self.tax_rate = code.rate
        self.net_amount = (self.delivery_line.quantity * self.unit_price).quantize(Decimal("0.0001"))
        self.tax_amount = (self.net_amount * self.tax_rate / Decimal("100")).quantize(Decimal("0.0001"))
        self.total_amount = self.net_amount + self.tax_amount

    def save(self, *args, **kwargs):
        if self.invoice_id and SalesInvoice.objects.filter(pk=self.invoice_id, status=SalesInvoice.Status.POSTED).exists():
            raise ValidationError("لا يمكن تعديل سطور فاتورة مبيعات مرحلة.")
        self.calculate()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.invoice.status == SalesInvoice.Status.POSTED:
            raise ValidationError("لا يمكن حذف سطور فاتورة مبيعات مرحلة.")
        return super().delete(*args, **kwargs)


class CustomerReceipt(models.Model):
    class Method(models.TextChoices):
        CASH = "cash", "نقدي"
        BANK = "bank", "تحويل بنكي"

    class Status(models.TextChoices):
        DRAFT = "draft", "مسودة"
        POSTED = "posted", "مرحّل"

    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="customer_receipts")
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="receipts")
    invoice = models.ForeignKey(SalesInvoice, on_delete=models.PROTECT, related_name="receipts")
    receipt_number = models.CharField(max_length=40)
    receipt_date = models.DateField()
    method = models.CharField(max_length=8, choices=Method.choices, default=Method.BANK)
    amount = models.DecimalField(max_digits=20, decimal_places=4, validators=[MinValueValidator(Decimal("0.0001"))])
    reference = models.CharField(max_length=120, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    journal_entry = models.OneToOneField("accounting.JournalEntry", null=True, blank=True, on_delete=models.PROTECT, related_name="customer_receipt")
    created_by = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="created_customer_receipts")
    created_at = models.DateTimeField(auto_now_add=True)
    posted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-receipt_date", "-id")
        constraints = [models.UniqueConstraint(fields=("company", "receipt_number"), name="uq_customer_receipt_company_no")]
        indexes = [models.Index(fields=("company", "status", "receipt_date"), name="sales_receipt_co_status_idx")]

    @property
    def company_id_for_permission(self):
        return self.company_id

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk, status=self.Status.POSTED).exists():
            raise ValidationError("سند قبض العميل المرحّل غير قابل للتعديل.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.status == self.Status.POSTED:
            raise ValidationError("لا يمكن حذف سند قبض مرحّل.")
        return super().delete(*args, **kwargs)

    def clean(self):
        super().clean()
        if self.customer_id and self.customer.company_id != self.company_id:
            raise ValidationError({"customer": "العميل لا يتبع الشركة المحددة."})
        if self.invoice_id and (
            self.invoice.company_id != self.company_id or self.invoice.customer_id != self.customer_id
        ):
            raise ValidationError({"invoice": "الفاتورة لا تتبع العميل والشركة المحددين."})
