"""نماذج دورة المبيعات والتسليم والفوترة وتحصيلات العملاء."""

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, MaxValueValidator
from django.db import models

from accounting.models import ChartOfAccount, TaxCode
from core.money import round_currency_amount
from companies.models import Branch, Company, Currency, ExchangeRate
from customers.models import Customer, CustomerDiscountRule
from inventory.models import Product, StockMovement, StockMovementLine, Warehouse


class SalesDelivery(models.Model):
    """يمثل إذن تسليم مرتبطًا بحركة صرف مخزنية."""
    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="sales_deliveries")
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="sales_deliveries")
    warehouse = models.ForeignKey(Warehouse, on_delete=models.PROTECT, related_name="sales_deliveries")
    stock_movement = models.OneToOneField(StockMovement, on_delete=models.PROTECT, related_name="sales_delivery")
    order = models.ForeignKey("sales.SalesOrder", null=True, blank=True, on_delete=models.PROTECT, related_name="deliveries")
    delivery_number = models.CharField(max_length=40)
    delivery_date = models.DateField()
    customer_reference = models.CharField(max_length=100, blank=True)
    notes = models.TextField(blank=True)
    quotation_snapshot = models.JSONField(default=dict, blank=True, editable=False)
    order_snapshot = models.JSONField(default=dict, blank=True, editable=False)
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





class SalesDeliveryAllocation(models.Model):
    """ربط بند إذن التسليم ببند أمر البيع لمنع تجاوز الكمية المتفق عليها."""
    delivery = models.ForeignKey(SalesDelivery, on_delete=models.PROTECT, related_name="order_allocations")
    order_line = models.ForeignKey("sales.SalesOrderLine", on_delete=models.PROTECT, related_name="delivery_allocations")
    movement_line = models.OneToOneField(StockMovementLine, on_delete=models.PROTECT, related_name="sales_order_allocation")
    quantity = models.DecimalField(max_digits=18, decimal_places=4, validators=[MinValueValidator(Decimal("0.0001"))])

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("delivery", "order_line"), name="uq_delivery_order_line"),
        ]

    def clean(self):
        super().clean()
        if self.delivery.order_id != self.order_line.order_id:
            raise ValidationError("بند أمر البيع لا يتبع أمر التسليم المحدد.")
        if self.movement_line.product_id != self.order_line.product_id:
            raise ValidationError("الصنف في إذن التسليم لا يطابق بند أمر البيع.")
        if self.movement_line.quantity != self.quantity:
            raise ValidationError("كمية الربط لا تطابق كمية حركة المخزون.")


class SalesQuotation(models.Model):
    """عرض سعر تجاري لا ينشئ حركة مخزون أو قيدًا محاسبيًا."""
    class Status(models.TextChoices):
        DRAFT = "draft", "مسودة"
        SENT = "sent", "مرسل"
        ACCEPTED = "accepted", "مقبول"
        REJECTED = "rejected", "مرفوض"
        EXPIRED = "expired", "منتهي"
        CONVERTED = "converted", "تم تحويله"

    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="sales_quotations")
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="sales_quotations")
    quotation_number = models.CharField(max_length=80)
    quotation_date = models.DateField()
    valid_until = models.DateField()
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT)
    net_total = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    discount_total = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    tax_total = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    grand_total = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    notes = models.TextField(blank=True)
    converted_delivery = models.OneToOneField(
        SalesDelivery, null=True, blank=True, on_delete=models.PROTECT,
        related_name="source_quotation",
    )
    created_by = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="created_sales_quotations")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    converted_at = models.DateTimeField(null=True, blank=True)
    print_template_snapshot = models.JSONField(default=dict, blank=True, editable=False)

    class Meta:
        ordering = ("-quotation_date", "-id")
        constraints = [
            models.UniqueConstraint(fields=("company", "quotation_number"), name="uq_sales_quote_company_no"),
            models.CheckConstraint(condition=models.Q(valid_until__gte=models.F("quotation_date")), name="ck_sales_quote_validity"),
        ]
        indexes = [models.Index(fields=("company", "status", "quotation_date"), name="sales_quote_co_status_idx")]

    def clean(self):
        super().clean()
        if self.customer_id and self.customer.company_id != self.company_id:
            raise ValidationError({"customer": "العميل لا يتبع الشركة المحددة."})
        if self.valid_until and self.quotation_date and self.valid_until < self.quotation_date:
            raise ValidationError({"valid_until": "تاريخ الصلاحية لا يسبق تاريخ العرض."})

    def save(self, *args, **kwargs):
        if self.pk:
            old = type(self).objects.get(pk=self.pk)
            if old.status == self.Status.CONVERTED:
                raise ValidationError("عرض السعر المحول لا يمكن تعديله.")
            if old.converted_delivery_id and self.converted_delivery_id != old.converted_delivery_id:
                raise ValidationError("رابط التحويل لا يمكن تغييره.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.status != self.Status.DRAFT or self.converted_delivery_id:
            raise ValidationError("لا يمكن حذف عرض سعر مرسل أو مقبول أو محول.")
        return super().delete(*args, **kwargs)


class SalesQuotationLine(models.Model):
    """بند عرض السعر وأسعاره وخصمه وضريبته وقت إعداد العرض."""
    quotation = models.ForeignKey(SalesQuotation, on_delete=models.PROTECT, related_name="lines")
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="sales_quotation_lines")
    quantity = models.DecimalField(max_digits=18, decimal_places=4, validators=[MinValueValidator(Decimal("0.0001"))])
    unit_price = models.DecimalField(max_digits=18, decimal_places=4, validators=[MinValueValidator(0)])
    tax_rate = models.DecimalField(max_digits=7, decimal_places=4, default=0, validators=[MinValueValidator(0), MaxValueValidator(100)])
    tax_code = models.ForeignKey(TaxCode, null=True, blank=True, on_delete=models.PROTECT, related_name="sales_quotation_lines")
    discount_percent = models.DecimalField(max_digits=7, decimal_places=4, default=0, validators=[MinValueValidator(0), MaxValueValidator(100)])
    discount_rule = models.ForeignKey(CustomerDiscountRule, null=True, blank=True, on_delete=models.PROTECT, related_name="sales_quotation_lines")
    discount_amount = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    net_amount = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    tax_amount = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    total_amount = models.DecimalField(max_digits=20, decimal_places=4, default=0)

    class Meta:
        ordering = ("id",)

    def save(self, *args, **kwargs):
        if self.quotation_id and self.quotation.status != SalesQuotation.Status.DRAFT:
            raise ValidationError("لا يمكن تعديل بنود عرض سعر غير مسودة.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.quotation.status != SalesQuotation.Status.DRAFT:
            raise ValidationError("لا يمكن حذف بنود عرض سعر غير مسودة.")
        return super().delete(*args, **kwargs)

    def calculate(self):
        if self.product.company_id != self.quotation.company_id:
            raise ValidationError({"product": "الصنف لا يتبع شركة عرض السعر."})
        if self.tax_code_id:
            code = self.tax_code
            if (not code.is_active or code.company_id != self.quotation.company_id
                    or code.country != self.quotation.company.country
                    or code.category == TaxCode.Category.REVERSE_CHARGE
                    or code.effective_from > self.quotation.quotation_date
                    or (code.effective_to and code.effective_to < self.quotation.quotation_date)):
                raise ValidationError({"tax_code": "كود الضريبة غير صالح لعرض السعر."})
            self.tax_rate = code.rate
        gross = (self.quantity * self.unit_price).quantize(Decimal("0.0001"))
        self.discount_amount = (gross * self.discount_percent / Decimal("100")).quantize(Decimal("0.0001"))
        self.net_amount = gross - self.discount_amount
        self.tax_amount = (self.net_amount * self.tax_rate / Decimal("100")).quantize(Decimal("0.0001"))
        self.total_amount = self.net_amount + self.tax_amount


class SalesInvoice(models.Model):
    """يمثل فاتورة العميل وإجماليات البيع والضريبة والاعتماد."""
    class Status(models.TextChoices):
        DRAFT = "draft", "مسودة"
        POSTED = "posted", "مرحّلة"

    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="sales_invoices")
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="sales_invoices")
    delivery = models.OneToOneField(SalesDelivery, null=True, blank=True, on_delete=models.PROTECT, related_name="invoice")
    branch = models.ForeignKey(Branch, null=True, blank=True, on_delete=models.PROTECT, related_name="sales_invoices")
    invoice_number = models.CharField(max_length=80)
    invoice_date = models.DateField()
    transaction_currency = models.ForeignKey(Currency, null=True, blank=True, on_delete=models.PROTECT, related_name="sales_invoices")
    exchange_rate = models.DecimalField(max_digits=20, decimal_places=10, null=True, blank=True)
    exchange_rate_date = models.DateField(null=True, blank=True)
    transaction_net_total = models.DecimalField(max_digits=20, decimal_places=4, null=True, blank=True)
    transaction_tax_total = models.DecimalField(max_digits=20, decimal_places=4, null=True, blank=True)
    transaction_grand_total = models.DecimalField(max_digits=20, decimal_places=4, null=True, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    print_template_snapshot = models.JSONField(default=dict, blank=True, editable=False)
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
        if self.branch_id and self.branch.company_id != self.company_id:
            raise ValidationError({"branch": "الفرع لا يتبع الشركة المحددة."})
        if not self.delivery_id and not self.branch_id:
            raise ValidationError({"branch": "حدد فرع فاتورة الخدمة."})
        if self.delivery_id and self.branch_id and self.delivery.warehouse.branch_id and self.delivery.warehouse.branch_id != self.branch_id:
            raise ValidationError({"branch": "فرع الفاتورة يجب أن يطابق فرع إذن التسليم."})


class SalesInvoiceLine(models.Model):
    """يمثل بند بيع بضاعة مسلمة أو خدمة مفوترة مباشرة."""
    invoice = models.ForeignKey(SalesInvoice, on_delete=models.PROTECT, related_name="lines")
    delivery_line = models.ForeignKey(StockMovementLine, null=True, blank=True, on_delete=models.PROTECT, related_name="sales_invoice_lines")
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="sales_invoice_lines")
    quantity = models.DecimalField(max_digits=18, decimal_places=4, default=1, validators=[MinValueValidator(Decimal("0.0001"))])
    unit_price = models.DecimalField(max_digits=18, decimal_places=4, validators=[MinValueValidator(0)])
    transaction_unit_price = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True, validators=[MinValueValidator(0)])
    transaction_net_amount = models.DecimalField(max_digits=20, decimal_places=4, null=True, blank=True)
    transaction_tax_amount = models.DecimalField(max_digits=20, decimal_places=4, null=True, blank=True)
    transaction_total_amount = models.DecimalField(max_digits=20, decimal_places=4, null=True, blank=True)
    tax_rate = models.DecimalField(max_digits=7, decimal_places=4, default=0, validators=[MinValueValidator(0), MaxValueValidator(100)])
    tax_code = models.ForeignKey(TaxCode, null=True, blank=True, on_delete=models.PROTECT, related_name="sales_invoice_lines")
    discount_percent = models.DecimalField(max_digits=7, decimal_places=4, default=0, validators=[MinValueValidator(0), MaxValueValidator(100)])
    discount_rule = models.ForeignKey(CustomerDiscountRule, null=True, blank=True, on_delete=models.PROTECT, related_name="sales_invoice_lines")
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
        if self.product_id and self.product.company_id != self.invoice.company_id:
            raise ValidationError({"product": "الصنف أو الخدمة لا يتبع شركة الفاتورة."})
        if self.delivery_line_id:
            if self.delivery_line.product_id != self.product_id or self.product.product_type != Product.ProductType.GOODS:
                raise ValidationError({"delivery_line": "بند التسليم يجب أن يطابق صنفًا مخزنيًا."})
            if self.quantity <= 0 or self.quantity > self.delivery_line.quantity:
                raise ValidationError({"quantity": "كمية الفاتورة يجب أن تكون أكبر من صفر ولا تتجاوز كمية بند إذن التسليم."})
        elif self.product.product_type != Product.ProductType.SERVICE:
            raise ValidationError({"product": "الفوترة المباشرة متاحة للخدمات فقط."})
        if self.invoice.transaction_currency_id:
            if self.transaction_unit_price is None:
                raise ValidationError({"transaction_unit_price": "سعر الوحدة بالعملة الأجنبية مطلوب."})
            foreign_gross = (self.quantity * self.transaction_unit_price).quantize(Decimal("0.0001"))
            foreign_discount = (foreign_gross * self.discount_percent / Decimal("100")).quantize(Decimal("0.0001"))
            self.transaction_net_amount = foreign_gross - foreign_discount
            self.transaction_tax_amount = round_currency_amount(self.transaction_net_amount * self.tax_rate / Decimal("100"), self.invoice.transaction_currency)
            self.transaction_total_amount = self.transaction_net_amount + self.transaction_tax_amount
            self.unit_price = (self.transaction_unit_price * self.invoice.exchange_rate).quantize(Decimal("0.0001"))
            self.discount_amount = (foreign_discount * self.invoice.exchange_rate).quantize(Decimal("0.0001"))
            self.net_amount = (self.transaction_net_amount * self.invoice.exchange_rate).quantize(Decimal("0.0001"))
            self.tax_amount = round_currency_amount(self.transaction_tax_amount * self.invoice.exchange_rate, self.invoice.company.base_currency)
            self.total_amount = self.net_amount + self.tax_amount
        else:
            if self.transaction_unit_price is not None:
                raise ValidationError({"transaction_unit_price": "لا تستخدم سعر وحدة أجنبيًا مع فاتورة بالعملة الأساسية."})
            self.transaction_net_amount = self.transaction_tax_amount = self.transaction_total_amount = None
            gross_amount = (self.quantity * self.unit_price).quantize(Decimal("0.0001"))
            self.discount_amount = (gross_amount * self.discount_percent / Decimal("100")).quantize(Decimal("0.0001"))
            self.net_amount = gross_amount - self.discount_amount
            self.tax_amount = round_currency_amount(self.net_amount * self.tax_rate / Decimal("100"), self.invoice.company.base_currency)
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
    warehouse = models.ForeignKey(Warehouse, null=True, blank=True, on_delete=models.PROTECT, related_name="sales_returns")
    branch = models.ForeignKey(Branch, null=True, blank=True, on_delete=models.PROTECT, related_name="sales_returns")
    stock_movement = models.OneToOneField(StockMovement, null=True, blank=True, on_delete=models.PROTECT, related_name="sales_return")
    return_number = models.CharField(max_length=37)
    return_date = models.DateField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    net_total = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    tax_total = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    grand_total = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    transaction_net_total = models.DecimalField(max_digits=20, decimal_places=4, null=True, blank=True)
    transaction_tax_total = models.DecimalField(max_digits=20, decimal_places=4, null=True, blank=True)
    transaction_grand_total = models.DecimalField(max_digits=20, decimal_places=4, null=True, blank=True)
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
        if self.invoice_id and self.invoice.delivery_id:
            if not self.warehouse_id or self.warehouse.company_id != self.company_id or self.invoice.delivery.warehouse_id != self.warehouse_id:
                raise ValidationError({"warehouse": "مخزن المردود يجب أن يطابق مخزن إذن التسليم الأصلي."})
            if self.branch_id and self.warehouse.branch_id and self.branch_id != self.warehouse.branch_id:
                raise ValidationError({"branch": "فرع المردود لا يطابق فرع المخزن."})
        elif self.invoice_id:
            if self.warehouse_id or not self.branch_id or self.branch.company_id != self.company_id or self.branch_id != self.invoice.branch_id:
                raise ValidationError({"branch": "مردود الخدمة يجب أن يتبع فرع فاتورة الخدمة دون حركة مخزنية."})
        if self.stock_movement_id and (
            not self.warehouse_id or self.stock_movement.company_id != self.company_id
            or self.stock_movement.destination_warehouse_id != self.warehouse_id
            or self.stock_movement.movement_type != StockMovement.MovementType.RECEIPT
        ):
            raise ValidationError({"stock_movement": "حركة الاستلام لا تطابق مردود المبيعات."})
        if self.invoice_id and not self.invoice.delivery_id and self.stock_movement_id:
            raise ValidationError({"stock_movement": "مردود الخدمة لا ينشئ حركة مخزنية."})
        if self.invoice_id and self.return_date and self.return_date < self.invoice.invoice_date:
            raise ValidationError({"return_date": "تاريخ المردود لا يجوز أن يسبق تاريخ الفاتورة الأصلية."})


class SalesReturnLine(models.Model):
    """بند مردود يحتفظ بلقطة السعر والضريبة والتكلفة من الفاتورة الأصلية."""
    sales_return = models.ForeignKey(SalesReturn, on_delete=models.PROTECT, related_name="lines")
    invoice_line = models.ForeignKey(SalesInvoiceLine, on_delete=models.PROTECT, related_name="return_lines")
    movement_line = models.OneToOneField(StockMovementLine, null=True, blank=True, on_delete=models.PROTECT, related_name="sales_return_line")
    quantity = models.DecimalField(max_digits=18, decimal_places=4, validators=[MinValueValidator(Decimal("0.0001"))])
    net_amount = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    tax_amount = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    total_amount = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    transaction_net_amount = models.DecimalField(max_digits=20, decimal_places=4, null=True, blank=True)
    transaction_tax_amount = models.DecimalField(max_digits=20, decimal_places=4, null=True, blank=True)
    transaction_total_amount = models.DecimalField(max_digits=20, decimal_places=4, null=True, blank=True)
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


class SalesDocumentTemplate(models.Model):
    """قوالب طباعة مستقلة لكل شركة ونوع مستند."""
    class DocumentType(models.TextChoices):
        QUOTATION = "quotation", "عرض سعر"
        INVOICE = "invoice", "فاتورة مبيعات"
        DELIVERY = "delivery", "إذن تسليم"
        RECEIPT = "receipt", "سند قبض"
        RETURN = "return", "مردود مبيعات"
        PURCHASE_REQ = "purchase_req", "طلب احتياج"
        PURCHASE_RFQ = "purchase_rfq", "طلب عروض أسعار"
        SUPPLIER_QUOTE = "supplier_quote", "عرض مورد"
        PURCHASE_ORDER = "purchase_order", "أمر شراء"
        SERVICE_ACCEPT = "service_accept", "محضر إنجاز خدمة"
        PURCHASE_RECEIPT = "purchase_receipt", "إذن استلام مشتريات"
        PURCHASE_INVOICE = "purchase_invoice", "فاتورة مورد"
        SUPPLIER_PAYMENT = "supplier_payment", "سند صرف مورد"
        SUPPLIER_RETURN = "supplier_return", "مرتجع مورد"
        SUPPLIER_REFUND = "supplier_refund", "استرداد من مورد"

    class Style(models.TextChoices):
        CLASSIC = "classic", "Classic"
        MODERN = "modern", "Modern"
        COMPACT = "compact", "Compact"

    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="sales_document_templates")
    document_type = models.CharField(max_length=16, choices=DocumentType.choices)
    name = models.CharField(max_length=80)
    style = models.CharField(max_length=12, choices=Style.choices, default=Style.CLASSIC)
    config = models.JSONField(default=dict, blank=True)
    is_default = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("document_type", "-is_default", "name")
        constraints = [
            models.UniqueConstraint(fields=("company", "document_type", "name"), name="uq_sales_template_company_type_name"),
            models.UniqueConstraint(fields=("company", "document_type"), condition=models.Q(is_default=True), name="uq_sales_template_default_type"),
        ]
        indexes = [models.Index(fields=("company", "document_type", "is_active"), name="sales_template_co_type_idx")]

    def clean(self):
        super().clean()
        if self.style not in self.Style.values:
            raise ValidationError({"style": "نمط القالب غير صالح."})
        if not isinstance(self.config, dict):
            raise ValidationError({"config": "إعدادات القالب يجب أن تكون كائن إعدادات."})



class SalesOrder(models.Model):
    """أمر بيع معتمد كأساس لتجهيز وتسليم البضائع على دفعات."""
    class Status(models.TextChoices):
        DRAFT = "draft", "مسودة"
        CONFIRMED = "confirmed", "معتمد"
        PARTIALLY_DELIVERED = "partially_delivered", "تم التسليم جزئيًا"
        DELIVERED = "delivered", "تم التسليم"
        CANCELLED = "cancelled", "ملغي"

    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="sales_orders")
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="sales_orders")
    warehouse = models.ForeignKey(Warehouse, on_delete=models.PROTECT, related_name="sales_orders")
    order_number = models.CharField(max_length=80)
    order_date = models.DateField()
    requested_delivery_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.DRAFT)
    net_total = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    discount_total = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    tax_total = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    grand_total = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="created_sales_orders")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-order_date", "-id")
        constraints = [
            models.UniqueConstraint(fields=("company", "order_number"), name="uq_sales_order_company_no"),
            models.CheckConstraint(
                condition=models.Q(requested_delivery_date__isnull=True) | models.Q(requested_delivery_date__gte=models.F("order_date")),
                name="ck_sales_order_delivery_date",
            ),
        ]
        indexes = [models.Index(fields=("company", "status", "order_date"), name="sales_order_co_status_idx")]

    @property
    def company_id_for_permission(self):
        return self.company_id

    def clean(self):
        super().clean()
        if self.customer_id and self.customer.company_id != self.company_id:
            raise ValidationError({"customer": "العميل لا يتبع الشركة المحددة."})
        if self.warehouse_id and self.warehouse.company_id != self.company_id:
            raise ValidationError({"warehouse": "المخزن لا يتبع الشركة المحددة."})
        if self.requested_delivery_date and self.order_date and self.requested_delivery_date < self.order_date:
            raise ValidationError({"requested_delivery_date": "تاريخ التسليم المطلوب لا يسبق تاريخ أمر البيع."})

    def save(self, *args, **kwargs):
        if self.pk:
            previous = type(self).objects.filter(pk=self.pk).values("status").first()
            if previous and previous["status"] != self.Status.DRAFT:
                update_fields = set(kwargs.get("update_fields") or ())
                if not update_fields or not update_fields.issubset({"status", "updated_at"}):
                    raise ValidationError("أمر البيع المعتمد لا يمكن تعديل بياناته.")
                allowed = {
                    self.Status.CONFIRMED: {self.Status.PARTIALLY_DELIVERED, self.Status.DELIVERED, self.Status.CANCELLED},
                    self.Status.PARTIALLY_DELIVERED: {self.Status.DELIVERED, self.Status.CANCELLED},
                    self.Status.DELIVERED: set(),
                    self.Status.CANCELLED: set(),
                }
                if self.status != previous["status"] and self.status not in allowed.get(previous["status"], set()):
                    raise ValidationError("انتقال حالة أمر البيع غير مسموح.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.status != self.Status.DRAFT:
            raise ValidationError("لا يمكن حذف أمر بيع معتمد.")
        return super().delete(*args, **kwargs)


class SalesOrderLine(models.Model):
    """بند أمر البيع؛ تحفظ الكمية والسعر والضريبة وقت الاتفاق مع العميل."""
    order = models.ForeignKey(SalesOrder, on_delete=models.PROTECT, related_name="lines")
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="sales_order_lines")
    quantity = models.DecimalField(max_digits=18, decimal_places=4, validators=[MinValueValidator(Decimal("0.0001"))])
    unit_price = models.DecimalField(max_digits=18, decimal_places=4, validators=[MinValueValidator(0)])
    tax_rate = models.DecimalField(max_digits=7, decimal_places=4, default=0, validators=[MinValueValidator(0), MaxValueValidator(100)])
    tax_code = models.ForeignKey(TaxCode, null=True, blank=True, on_delete=models.PROTECT, related_name="sales_order_lines")
    discount_percent = models.DecimalField(max_digits=7, decimal_places=4, default=0, validators=[MinValueValidator(0), MaxValueValidator(100)])
    discount_amount = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    net_amount = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    tax_amount = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    total_amount = models.DecimalField(max_digits=20, decimal_places=4, default=0)

    class Meta:
        ordering = ("id",)

    def calculate(self):
        if self.product.company_id != self.order.company_id or not self.product.is_active or self.product.product_type != Product.ProductType.GOODS:
            raise ValidationError({"product": "اختر صنفًا مخزنيًا نشطًا تابعًا لشركة أمر البيع."})
        if self.tax_code_id:
            code = self.tax_code
            if (not code.is_active or code.company_id != self.order.company_id
                    or code.country != self.order.company.country
                    or code.category == TaxCode.Category.REVERSE_CHARGE
                    or code.effective_from > self.order.order_date
                    or (code.effective_to and code.effective_to < self.order.order_date)):
                raise ValidationError({"tax_code": "كود الضريبة غير صالح لأمر البيع."})
            self.tax_rate = code.rate
        gross = (self.quantity * self.unit_price).quantize(Decimal("0.0001"))
        self.discount_amount = (gross * self.discount_percent / Decimal("100")).quantize(Decimal("0.0001"))
        self.net_amount = gross - self.discount_amount
        self.tax_amount = (self.net_amount * self.tax_rate / Decimal("100")).quantize(Decimal("0.0001"))
        self.total_amount = self.net_amount + self.tax_amount

    def save(self, *args, **kwargs):
        if self.order_id and self.order.status != SalesOrder.Status.DRAFT:
            raise ValidationError("لا يمكن تعديل بنود أمر بيع معتمد.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.order.status != SalesOrder.Status.DRAFT:
            raise ValidationError("لا يمكن حذف بنود أمر بيع معتمد.")
        return super().delete(*args, **kwargs)

class CustomerReceiptBatch(models.Model):
    """سند قبض واحد يوزع على عدة فواتير أو يسجل دفعة مقدمة غير مخصصة."""
    class Status(models.TextChoices):
        DRAFT = "draft", "مسودة"
        POSTED = "posted", "مرحّل"
        REVERSED = "reversed", "معكوس"

    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="customer_receipt_batches")
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="receipt_batches")
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name="customer_receipt_batches")
    receipt_number = models.CharField(max_length=40)
    receipt_date = models.DateField()
    method = models.CharField(max_length=8, choices=CustomerReceipt.Method.choices, default=CustomerReceipt.Method.BANK)
    payment_account = models.ForeignKey(
        ChartOfAccount, null=True, blank=True, on_delete=models.PROTECT,
        related_name="customer_receipt_batches",
    )
    amount = models.DecimalField(
        max_digits=20, decimal_places=4,
        validators=[MinValueValidator(Decimal("0.0001"))],
    )
    reference = models.CharField(max_length=120, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    journal_entry = models.OneToOneField(
        "accounting.JournalEntry", null=True, blank=True, on_delete=models.PROTECT,
        related_name="customer_receipt_batch",
    )
    reversal_journal_entry = models.OneToOneField(
        "accounting.JournalEntry", null=True, blank=True, on_delete=models.PROTECT,
        related_name="reversed_customer_receipt_batch",
    )
    reversal_date = models.DateField(null=True, blank=True)
    reversed_at = models.DateTimeField(null=True, blank=True)
    reversed_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.PROTECT,
        related_name="reversed_customer_receipt_batches",
    )
    created_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.PROTECT,
        related_name="created_customer_receipt_batches",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    posted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-receipt_date", "-id")
        constraints = [
            models.UniqueConstraint(
                fields=("company", "receipt_number"),
                name="uq_rcpt_batch_company_no",
            ),
        ]
        indexes = [
            models.Index(
                fields=("company", "status", "receipt_date"),
                name="sales_rcpt_batch_co_stat_idx",
            ),
            models.Index(fields=("branch", "receipt_date"), name="sales_rcpt_batch_branch_idx"),
        ]

    @property
    def company_id_for_permission(self):
        return self.company_id

    @property
    def allocated_amount(self):
        return self.allocations.aggregate(total=models.Sum("amount"))["total"] or Decimal("0")

    @property
    def unallocated_amount(self):
        return max(self.amount - self.allocated_amount, Decimal("0"))

    def clean(self):
        super().clean()
        if self.customer_id and self.customer.company_id != self.company_id:
            raise ValidationError({"customer": "العميل لا يتبع الشركة المحددة."})
        if self.branch_id and self.branch.company_id != self.company_id:
            raise ValidationError({"branch": "الفرع لا يتبع الشركة المحددة."})

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(
            pk=self.pk, status__in=(self.Status.POSTED, self.Status.REVERSED)
        ).exists():
            allowed = {
                "status", "reversal_journal_entry", "reversal_date",
                "reversed_at", "reversed_by",
            }
            fields = set(kwargs.get("update_fields") or ())
            if not fields or not fields.issubset(allowed):
                raise ValidationError("سند قبض العميل المرحّل غير قابل للتعديل.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.status != self.Status.DRAFT:
            raise ValidationError("لا يمكن حذف سند قبض مجمع مرحّل.")
        return super().delete(*args, **kwargs)


class CustomerReceiptBatchAllocation(models.Model):
    """تخصيص جزء من سند القبض المجمع إلى فاتورة عميل محددة."""
    batch = models.ForeignKey(
        CustomerReceiptBatch, on_delete=models.PROTECT, related_name="allocations",
    )
    invoice = models.ForeignKey(
        SalesInvoice, on_delete=models.PROTECT, related_name="receipt_batch_allocations",
    )
    amount = models.DecimalField(
        max_digits=20, decimal_places=4,
        validators=[MinValueValidator(Decimal("0.0001"))],
    )
    allocation_date = models.DateField()
    created_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.PROTECT,
        related_name="created_customer_receipt_batch_allocations",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("created_at", "id")
        indexes = [
            models.Index(fields=("invoice", "batch"), name="sales_rcpt_alloc_invoice_idx"),
        ]

    def clean(self):
        super().clean()
        if self.batch_id and self.invoice_id:
            if (
                self.batch.company_id != self.invoice.company_id
                or self.batch.customer_id != self.invoice.customer_id
            ):
                raise ValidationError({"invoice": "الفاتورة لا تتبع العميل والشركة المحددين في سند القبض."})
            if self.invoice.transaction_currency_id:
                raise ValidationError("التخصيص المجمع متاح لفواتير العملة الأساسية فقط.")

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("سطر تخصيص سند القبض غير قابل للتعديل؛ أضف تخصيصًا جديدًا.")
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.batch.status != CustomerReceiptBatch.Status.DRAFT:
            raise ValidationError("لا يمكن حذف تخصيص من سند قبض مرحّل.")
        return super().delete(*args, **kwargs)

class CustomerRefund(models.Model):
    """استرداد نقدي للعميل مقابل رصيد دائن ناتج عن سند قبض مجمع."""

    class Status(models.TextChoices):
        DRAFT = "draft", "مسودة"
        POSTED = "posted", "مرحّل"
        REVERSED = "reversed", "معكوس"

    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="customer_refunds")
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="refunds")
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name="customer_refunds")
    source_receipt_batch = models.ForeignKey(
        "CustomerReceiptBatch", on_delete=models.PROTECT, related_name="refunds",
    )
    treasury_account = models.ForeignKey(
        "treasury.TreasuryAccount", on_delete=models.PROTECT, related_name="customer_refunds",
    )
    treasury_transaction = models.ForeignKey(
        "treasury.TreasuryTransaction", null=True, blank=True, on_delete=models.PROTECT,
        related_name="customer_refunds",
    )
    reversal_treasury_transaction = models.ForeignKey(
        "treasury.TreasuryTransaction", null=True, blank=True, on_delete=models.PROTECT,
        related_name="reversed_customer_refunds",
    )
    reversal_journal_entry = models.OneToOneField(
        "accounting.JournalEntry", null=True, blank=True, on_delete=models.PROTECT,
        related_name="customer_refund_reversal",
    )
    refund_number = models.CharField(max_length=40)
    refund_date = models.DateField()
    amount = models.DecimalField(
        max_digits=20, decimal_places=4, validators=[MinValueValidator(Decimal("0.0001"))],
    )
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    reversal_date = models.DateField(null=True, blank=True)
    created_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.PROTECT,
        related_name="created_customer_refunds",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-refund_date", "-id")
        constraints = [
            models.UniqueConstraint(fields=("company", "refund_number"), name="uq_customer_refund_company_no"),
        ]
        indexes = [
            models.Index(fields=("company", "status", "refund_date"), name="sales_refund_co_status_idx"),
        ]

    @property
    def company_id_for_permission(self):
        return self.company_id

    def clean(self):
        super().clean()
        if self.customer_id and self.customer.company_id != self.company_id:
            raise ValidationError({"customer": "العميل لا يتبع الشركة المحددة."})
        if self.branch_id and self.branch.company_id != self.company_id:
            raise ValidationError({"branch": "الفرع لا يتبع الشركة المحددة."})
        if self.source_receipt_batch_id and (
            self.source_receipt_batch.company_id != self.company_id
            or self.source_receipt_batch.customer_id != self.customer_id
            or self.source_receipt_batch.branch_id != self.branch_id
        ):
            raise ValidationError({"source_receipt_batch": "سند القبض لا يطابق الشركة أو العميل أو الفرع."})
        if self.treasury_account_id and self.treasury_account.company_id != self.company_id:
            raise ValidationError({"treasury_account": "حساب الخزينة لا يتبع الشركة المحددة."})
        if self.treasury_transaction_id and (
            self.treasury_transaction.company_id != self.company_id
            or self.treasury_transaction.branch_id != self.branch_id
        ):
            raise ValidationError({"treasury_transaction": "حركة الخزينة لا تطابق الشركة أو الفرع."})

    def save(self, *args, **kwargs):
        if self.pk:
            current_status = type(self).objects.filter(pk=self.pk).values_list("status", flat=True).first()
            if current_status == self.Status.REVERSED:
                raise ValidationError("مردود العميل المعكوس غير قابل للتعديل.")
            if current_status == self.Status.POSTED:
                reversal_fields = {
                    "reversal_journal_entry", "reversal_treasury_transaction",
                    "reversal_date", "status",
                }
                update_fields = set(kwargs.get("update_fields") or ())
                if self.status != self.Status.REVERSED or not update_fields or not update_fields.issubset(reversal_fields):
                    raise ValidationError("مردود العميل المرحّل غير قابل للتعديل إلا من خلال إجراء العكس.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.status in (self.Status.POSTED, self.Status.REVERSED):
            raise ValidationError("لا يمكن حذف مردود عميل مرحّل أو معكوس.")
        return super().delete(*args, **kwargs)

