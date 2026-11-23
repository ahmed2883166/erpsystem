"""نماذج دورة المبيعات والتسليم والفوترة وتحصيلات العملاء."""

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, MaxValueValidator
from django.db import models

from accounting.models import ChartOfAccount, TaxCode
from companies.models import Company, Currency, ExchangeRate
from customers.models import Customer
from inventory.models import StockMovement, StockMovementLine, Warehouse


class SalesDelivery(models.Model):
    """يمثل إذن تسليم مرتبطًا بحركة صرف مخزنية."""
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
    """يمثل فاتورة العميل وإجماليات البيع والضريبة والاعتماد."""
    class Status(models.TextChoices):
        DRAFT = "draft", "مسودة"
        POSTED = "posted", "مرحّلة"

    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="sales_invoices")
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="sales_invoices")
    delivery = models.OneToOneField(SalesDelivery, on_delete=models.PROTECT, related_name="invoice")
    invoice_number = models.CharField(max_length=80)
    invoice_date = models.DateField()
    transaction_currency = models.ForeignKey(Currency, null=True, blank=True, on_delete=models.PROTECT, related_name="sales_invoices")
    exchange_rate = models.DecimalField(max_digits=20, decimal_places=10, null=True, blank=True)
    exchange_rate_date = models.DateField(null=True, blank=True)
    transaction_net_total = models.DecimalField(max_digits=20, decimal_places=4, null=True, blank=True)
    transaction_tax_total = models.DecimalField(max_digits=20, decimal_places=4, null=True, blank=True)
    transaction_grand_total = models.DecimalField(max_digits=20, decimal_places=4, null=True, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    net_total = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    discount_total = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    discount_approval_limit = models.DecimalField(max_digits=7, decimal_places=4, null=True, blank=True)
    invoice_approval_limit = models.DecimalField(max_digits=20, decimal_places=4, null=True, blank=True)
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
        fx_values = (self.transaction_currency_id, self.exchange_rate, self.exchange_rate_date)
        if any(value is not None for value in fx_values):
            if any(value is None for value in fx_values):
                raise ValidationError("بيانات العملة الأجنبية وسعر الصرف يجب إدخالها كاملة.")
            if not self.transaction_currency.is_active or self.transaction_currency.code == self.company.base_currency.code:
                raise ValidationError({"transaction_currency": "اختر عملة أجنبية نشطة ومختلفة عن العملة الأساسية."})
            if self.exchange_rate <= 0 or self.exchange_rate_date > self.invoice_date:
                raise ValidationError("سعر الصرف يجب أن يكون موجبًا وتاريخه لا يتجاوز تاريخ الفاتورة.")
            if not ExchangeRate.objects.filter(company=self.company, currency=self.transaction_currency, rate_date=self.exchange_rate_date, rate=self.exchange_rate).exists():
                raise ValidationError("سعر الصرف لا يطابق سعرًا معتمدًا للشركة والتاريخ.")
        elif any(value is not None for value in (self.transaction_net_total, self.transaction_tax_total, self.transaction_grand_total)):
            raise ValidationError("إجماليات العملة الأجنبية لا تقبل دون لقطة سعر صرف.")
        if self.customer_id and self.customer.company_id != self.company_id:
            raise ValidationError({"customer": "العميل لا يتبع الشركة المحددة."})
        if self.delivery_id and (
            self.delivery.company_id != self.company_id or self.delivery.customer_id != self.customer_id
        ):
            raise ValidationError({"delivery": "إذن التسليم لا يتبع العميل والشركة المحددين."})


class SalesInvoiceLine(models.Model):
    """يمثل بند البيع المرتبط بصنف تم تسليمه."""
    invoice = models.ForeignKey(SalesInvoice, on_delete=models.PROTECT, related_name="lines")
    delivery_line = models.OneToOneField(StockMovementLine, on_delete=models.PROTECT, related_name="sales_invoice_line")
    unit_price = models.DecimalField(max_digits=18, decimal_places=4, validators=[MinValueValidator(0)])
    transaction_unit_price = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True, validators=[MinValueValidator(0)])
    transaction_net_amount = models.DecimalField(max_digits=20, decimal_places=4, null=True, blank=True)
    transaction_tax_amount = models.DecimalField(max_digits=20, decimal_places=4, null=True, blank=True)
    transaction_total_amount = models.DecimalField(max_digits=20, decimal_places=4, null=True, blank=True)
    tax_rate = models.DecimalField(max_digits=7, decimal_places=4, default=0, validators=[MinValueValidator(0), MaxValueValidator(100)])
    tax_code = models.ForeignKey(TaxCode, null=True, blank=True, on_delete=models.PROTECT, related_name="sales_invoice_lines")
    discount_percent = models.DecimalField(max_digits=7, decimal_places=4, default=0, validators=[MinValueValidator(0), MaxValueValidator(100)])
    discount_amount = models.DecimalField(max_digits=20, decimal_places=4, default=0)
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
        if self.invoice.transaction_currency_id:
            if self.transaction_unit_price is None:
                raise ValidationError({"transaction_unit_price": "سعر الوحدة بالعملة الأجنبية مطلوب."})
            foreign_gross = (self.delivery_line.quantity * self.transaction_unit_price).quantize(Decimal("0.0001"))
            foreign_discount = (foreign_gross * self.discount_percent / Decimal("100")).quantize(Decimal("0.0001"))
            self.transaction_net_amount = foreign_gross - foreign_discount
            self.transaction_tax_amount = (self.transaction_net_amount * self.tax_rate / Decimal("100")).quantize(Decimal("0.0001"))
            self.transaction_total_amount = self.transaction_net_amount + self.transaction_tax_amount
            self.unit_price = (self.transaction_unit_price * self.invoice.exchange_rate).quantize(Decimal("0.0001"))
            self.discount_amount = (foreign_discount * self.invoice.exchange_rate).quantize(Decimal("0.0001"))
            self.net_amount = (self.transaction_net_amount * self.invoice.exchange_rate).quantize(Decimal("0.0001"))
            self.tax_amount = (self.transaction_tax_amount * self.invoice.exchange_rate).quantize(Decimal("0.0001"))
            self.total_amount = self.net_amount + self.tax_amount
        else:
            if self.transaction_unit_price is not None:
                raise ValidationError({"transaction_unit_price": "لا تستخدم سعر وحدة أجنبيًا مع فاتورة بالعملة الأساسية."})
            self.transaction_net_amount = self.transaction_tax_amount = self.transaction_total_amount = None
            gross_amount = (self.delivery_line.quantity * self.unit_price).quantize(Decimal("0.0001"))
            self.discount_amount = (gross_amount * self.discount_percent / Decimal("100")).quantize(Decimal("0.0001"))
            self.net_amount = gross_amount - self.discount_amount
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
    """يمثل تحصيلًا من العميل ووسيلة إيداعه وحالة ترحيله."""
    class Method(models.TextChoices):
        CASH = "cash", "نقدي"
        BANK = "bank", "تحويل بنكي"

    class Status(models.TextChoices):
        DRAFT = "draft", "مسودة"
        POSTED = "posted", "مرحّل"
        REVERSED = "reversed", "معكوس"

    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="customer_receipts")
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="receipts")
    invoice = models.ForeignKey(SalesInvoice, on_delete=models.PROTECT, related_name="receipts")
    receipt_number = models.CharField(max_length=40)
    receipt_date = models.DateField()
    method = models.CharField(max_length=8, choices=Method.choices, default=Method.BANK)
    payment_account = models.ForeignKey(ChartOfAccount, null=True, blank=True, on_delete=models.PROTECT, related_name="customer_receipts")
    amount = models.DecimalField(max_digits=20, decimal_places=4, validators=[MinValueValidator(Decimal("0.0001"))])
    transaction_currency = models.ForeignKey(Currency, null=True, blank=True, on_delete=models.PROTECT, related_name="customer_receipts")
    transaction_amount = models.DecimalField(max_digits=20, decimal_places=4, null=True, blank=True)
    exchange_rate = models.DecimalField(max_digits=20, decimal_places=10, null=True, blank=True)
    exchange_rate_date = models.DateField(null=True, blank=True)
    carrying_base_amount = models.DecimalField(max_digits=20, decimal_places=4, null=True, blank=True)
    historical_carrying_base_amount = models.DecimalField(max_digits=20, decimal_places=4, null=True, blank=True)
    realized_fx_gain = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    realized_fx_loss = models.DecimalField(max_digits=20, decimal_places=4, default=0)
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
        if self.pk and type(self).objects.filter(pk=self.pk, status__in=(self.Status.POSTED, self.Status.REVERSED)).exists():
            raise ValidationError("سند قبض العميل المرحّل غير قابل للتعديل.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.status in (self.Status.POSTED, self.Status.REVERSED):
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


class SalesReturn(models.Model):
    """إشعار مردود مبيعات مرتبط بفاتورة أصلية وحركة استلام مخزني."""
    class Status(models.TextChoices):
        DRAFT = "draft", "مسودة"
        POSTED = "posted", "مرحّل"
        REVERSED = "reversed", "معكوس"

    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="sales_returns")
    invoice = models.ForeignKey(SalesInvoice, on_delete=models.PROTECT, related_name="returns")
    warehouse = models.ForeignKey(Warehouse, on_delete=models.PROTECT, related_name="sales_returns")
    stock_movement = models.OneToOneField(StockMovement, on_delete=models.PROTECT, related_name="sales_return")
    return_number = models.CharField(max_length=37)
    return_date = models.DateField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    net_total = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    tax_total = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    grand_total = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    journal_entry = models.OneToOneField("accounting.JournalEntry", null=True, blank=True, on_delete=models.PROTECT, related_name="sales_return")
    cost_journal_entry = models.OneToOneField("accounting.JournalEntry", null=True, blank=True, on_delete=models.PROTECT, related_name="sales_return_cost")
    reversal_stock_movement = models.OneToOneField("inventory.StockMovement", null=True, blank=True, on_delete=models.PROTECT, related_name="sales_return_reversal")
    reversal_journal_entry = models.OneToOneField("accounting.JournalEntry", null=True, blank=True, on_delete=models.PROTECT, related_name="sales_return_reversal")
    reversal_date = models.DateField(null=True, blank=True)
    reversed_at = models.DateTimeField(null=True, blank=True)
    reversed_by = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="reversed_sales_returns")
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="created_sales_returns")
    created_at = models.DateTimeField(auto_now_add=True)
    posted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-return_date", "-id")
        constraints = [models.UniqueConstraint(fields=("company", "return_number"), name="uq_sales_return_company_no")]
        indexes = [models.Index(fields=("company", "status", "return_date"), name="sales_return_co_status_idx")]

    @property
    def company_id_for_permission(self):
        return self.company_id

    def save(self, *args, **kwargs):
        if self.pk:
            current_status = type(self).objects.filter(pk=self.pk).values_list("status", flat=True).first()
            if current_status == self.Status.REVERSED:
                raise ValidationError("مردود المبيعات المعكوس غير قابل للتعديل.")
            if current_status == self.Status.POSTED:
                reversal_fields = {
                    "reversal_stock_movement", "reversal_journal_entry", "reversal_date",
                    "reversed_at", "reversed_by", "status",
                }
                update_fields = set(kwargs.get("update_fields") or ())
                if self.status != self.Status.REVERSED or not update_fields or not update_fields.issubset(reversal_fields):
                    raise ValidationError("مردود المبيعات المرحّل غير قابل للتعديل إلا من خلال إجراء العكس.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.status in (self.Status.POSTED, self.Status.REVERSED):
            raise ValidationError("لا يمكن حذف مردود مبيعات مرحّل أو معكوس.")
        return super().delete(*args, **kwargs)

    def clean(self):
        super().clean()
        if self.invoice_id and (
            self.invoice.company_id != self.company_id
            or self.invoice.status != SalesInvoice.Status.POSTED
        ):
            raise ValidationError({"invoice": "يجب اختيار فاتورة مبيعات مرحلة تابعة للشركة."})
        if self.warehouse_id and (
            self.warehouse.company_id != self.company_id
            or (self.invoice_id and self.invoice.delivery.warehouse_id != self.warehouse_id)
        ):
            raise ValidationError({"warehouse": "مخزن المردود يجب أن يطابق مخزن إذن التسليم الأصلي."})
        if self.stock_movement_id and (
            self.stock_movement.company_id != self.company_id
            or self.stock_movement.destination_warehouse_id != self.warehouse_id
            or self.stock_movement.movement_type != StockMovement.MovementType.RECEIPT
        ):
            raise ValidationError({"stock_movement": "حركة الاستلام لا تطابق مردود المبيعات."})
        if self.invoice_id and self.return_date and self.return_date < self.invoice.invoice_date:
            raise ValidationError({"return_date": "تاريخ المردود لا يجوز أن يسبق تاريخ الفاتورة الأصلية."})


class SalesReturnLine(models.Model):
    """بند مردود يحتفظ بلقطة السعر والضريبة والتكلفة من الفاتورة الأصلية."""
    sales_return = models.ForeignKey(SalesReturn, on_delete=models.PROTECT, related_name="lines")
    invoice_line = models.ForeignKey(SalesInvoiceLine, on_delete=models.PROTECT, related_name="return_lines")
    movement_line = models.OneToOneField(StockMovementLine, on_delete=models.PROTECT, related_name="sales_return_line")
    quantity = models.DecimalField(max_digits=18, decimal_places=4, validators=[MinValueValidator(Decimal("0.0001"))])
    net_amount = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    tax_amount = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    total_amount = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    cost_amount = models.DecimalField(max_digits=20, decimal_places=4, default=0)

    class Meta:
        ordering = ("id",)
        constraints = [models.UniqueConstraint(fields=("sales_return", "invoice_line"), name="uq_sales_return_invoice_line")]

    def save(self, *args, **kwargs):
        if self.sales_return_id and SalesReturn.objects.filter(pk=self.sales_return_id, status__in=(SalesReturn.Status.POSTED, SalesReturn.Status.REVERSED)).exists():
            raise ValidationError("لا يمكن تعديل بنود مردود مبيعات مرحّل أو معكوس.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.sales_return.status in (SalesReturn.Status.POSTED, SalesReturn.Status.REVERSED):
            raise ValidationError("لا يمكن حذف بنود مردود مبيعات مرحّل أو معكوس.")
        return super().delete(*args, **kwargs)
