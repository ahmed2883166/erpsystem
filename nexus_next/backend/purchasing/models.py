"""نماذج دورة المشتريات من أوامر الشراء والاستلام إلى فواتير الموردين والمدفوعات."""

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, MaxValueValidator
from django.db import models

from accounting.models import ChartOfAccount, TaxCode
from companies.models import Company, Currency, ExchangeRate
from inventory.models import StockMovement, StockMovementLine, Warehouse
from suppliers.models import Supplier


class PurchaseReceipt(models.Model):
    """يمثل مستند استلام مشتريات المرتبط بحركة المخزون."""
    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="purchase_receipts")
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name="purchase_receipts")
    purchase_order = models.ForeignKey("PurchaseOrder", null=True, blank=True, on_delete=models.PROTECT, related_name="receipts")
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

    def save(self, *args, **kwargs):
        if self.pk:
            previous = type(self).objects.select_related("stock_movement").filter(pk=self.pk).first()
            if previous and previous.stock_movement.status != StockMovement.Status.DRAFT:
                raise ValidationError("إذن الاستلام المرحّل غير قابل للتعديل.")
        return super().save(*args, **kwargs)

    def __str__(self):
        return self.receipt_number


class PurchaseInvoice(models.Model):
    """يمثل فاتورة المورد وإجمالياتها وحالة ترحيلها."""
    class Status(models.TextChoices):
        DRAFT = "draft", "مسودة"
        POSTED = "posted", "مرحّلة"

    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="purchase_invoices")
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name="purchase_invoices")
    receipt = models.OneToOneField(PurchaseReceipt, on_delete=models.PROTECT, related_name="invoice")
    invoice_number = models.CharField(max_length=80)
    invoice_date = models.DateField()
    transaction_currency = models.ForeignKey(Currency, null=True, blank=True, on_delete=models.PROTECT, related_name="purchase_invoices")
    exchange_rate = models.DecimalField(max_digits=20, decimal_places=10, null=True, blank=True)
    exchange_rate_date = models.DateField(null=True, blank=True)
    transaction_net_total = models.DecimalField(max_digits=20, decimal_places=4, null=True, blank=True)
    transaction_tax_total = models.DecimalField(max_digits=20, decimal_places=4, null=True, blank=True)
    transaction_grand_total = models.DecimalField(max_digits=20, decimal_places=4, null=True, blank=True)
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
    """يمثل بندًا ماليًا وضريبيًا داخل فاتورة المشتريات."""
    invoice = models.ForeignKey(PurchaseInvoice, on_delete=models.PROTECT, related_name="lines")
    receipt_line = models.OneToOneField(StockMovementLine, on_delete=models.PROTECT, related_name="purchase_invoice_line")
    unit_price = models.DecimalField(max_digits=18, decimal_places=4, validators=[MinValueValidator(0)])
    transaction_unit_price = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True, validators=[MinValueValidator(0)])
    transaction_net_amount = models.DecimalField(max_digits=20, decimal_places=4, null=True, blank=True)
    transaction_tax_amount = models.DecimalField(max_digits=20, decimal_places=4, null=True, blank=True)
    transaction_total_amount = models.DecimalField(max_digits=20, decimal_places=4, null=True, blank=True)
    tax_rate = models.DecimalField(max_digits=7, decimal_places=4, default=0, validators=[MinValueValidator(0), MaxValueValidator(100)])
    tax_code = models.ForeignKey(TaxCode, null=True, blank=True, on_delete=models.PROTECT, related_name="purchase_invoice_lines")
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
                    or not code.input_tax_recoverable
                    or code.category == TaxCode.Category.REVERSE_CHARGE
                    or code.effective_from > self.invoice.invoice_date
                    or (code.effective_to and code.effective_to < self.invoice.invoice_date)):
                raise ValidationError({"tax_code": "كود الضريبة غير صالح أو غير قابل للخصم لهذه الشركة أو تاريخ الفاتورة."})
            self.tax_rate = code.rate
        quantity = self.receipt_line.quantity
        if self.invoice.transaction_currency_id:
            if self.transaction_unit_price is None:
                raise ValidationError({"transaction_unit_price": "سعر الوحدة بالعملة الأجنبية مطلوب."})
            self.transaction_net_amount = (quantity * self.transaction_unit_price).quantize(Decimal("0.0001"))
            self.transaction_tax_amount = (self.transaction_net_amount * self.tax_rate / Decimal("100")).quantize(Decimal("0.0001"))
            self.transaction_total_amount = self.transaction_net_amount + self.transaction_tax_amount
            self.unit_price = (self.transaction_unit_price * self.invoice.exchange_rate).quantize(Decimal("0.0001"))
            self.net_amount = (self.transaction_net_amount * self.invoice.exchange_rate).quantize(Decimal("0.0001"))
            self.tax_amount = (self.transaction_tax_amount * self.invoice.exchange_rate).quantize(Decimal("0.0001"))
            self.total_amount = self.net_amount + self.tax_amount
        else:
            if self.transaction_unit_price is not None:
                raise ValidationError({"transaction_unit_price": "لا تستخدم سعر وحدة أجنبيًا مع فاتورة بالعملة الأساسية."})
            self.transaction_net_amount = self.transaction_tax_amount = self.transaction_total_amount = None
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


class SupplierPayment(models.Model):
    """يمثل دفعة مسددة للمورد وارتباطها بالحسابات."""
    class Method(models.TextChoices):
        CASH = "cash", "نقدي"
        BANK = "bank", "تحويل بنكي"

    class Status(models.TextChoices):
        DRAFT = "draft", "مسودة"
        POSTED = "posted", "مرحّل"

    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="supplier_payments")
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name="payments")
    invoice = models.ForeignKey(PurchaseInvoice, on_delete=models.PROTECT, related_name="payments")
    payment_number = models.CharField(max_length=40)
    payment_date = models.DateField()
    method = models.CharField(max_length=8, choices=Method.choices, default=Method.BANK)
    payment_account = models.ForeignKey(ChartOfAccount, null=True, blank=True, on_delete=models.PROTECT, related_name="supplier_payments")
    amount = models.DecimalField(max_digits=20, decimal_places=4, validators=[MinValueValidator(Decimal("0.0001"))])
    transaction_currency = models.ForeignKey(Currency, null=True, blank=True, on_delete=models.PROTECT, related_name="supplier_payments")
    transaction_amount = models.DecimalField(max_digits=20, decimal_places=4, null=True, blank=True)
    exchange_rate = models.DecimalField(max_digits=20, decimal_places=10, null=True, blank=True)
    exchange_rate_date = models.DateField(null=True, blank=True)
    carrying_base_amount = models.DecimalField(max_digits=20, decimal_places=4, null=True, blank=True)
    realized_fx_gain = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    realized_fx_loss = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    reference = models.CharField(max_length=120, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    journal_entry = models.OneToOneField("accounting.JournalEntry", null=True, blank=True, on_delete=models.PROTECT, related_name="supplier_payment")
    created_by = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="created_supplier_payments")
    created_at = models.DateTimeField(auto_now_add=True)
    posted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-payment_date", "-id")
        constraints = [
            models.UniqueConstraint(fields=("company", "payment_number"), name="uq_supplier_payment_company_no"),
        ]
        indexes = [models.Index(fields=("company", "status", "payment_date"), name="pur_payment_co_status_idx")]

    @property
    def company_id_for_permission(self):
        return self.company_id

    def clean(self):
        super().clean()
        if self.supplier_id and self.supplier.company_id != self.company_id:
            raise ValidationError({"supplier": "المورد لا يتبع الشركة المحددة."})
        if self.invoice_id and (
            self.invoice.company_id != self.company_id or self.invoice.supplier_id != self.supplier_id
        ):
            raise ValidationError({"invoice": "الفاتورة لا تتبع المورد والشركة المحددين."})

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk, status=self.Status.POSTED).exists():
            raise ValidationError("سند صرف المورد المرحّل غير قابل للتعديل.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.status == self.Status.POSTED:
            raise ValidationError("لا يمكن حذف سند صرف مورد مرحّل.")
        return super().delete(*args, **kwargs)


class PurchaseOrder(models.Model):
    """يمثل أمر شراء ومراحله قبل الاستلام والفوترة."""
    class Status(models.TextChoices):
        DRAFT = "draft", "مسودة"
        CONFIRMED = "confirmed", "معتمد"
        PARTIALLY_RECEIVED = "partially_received", "استلام جزئي"
        RECEIVED = "received", "مستلم بالكامل"
        CANCELLED = "cancelled", "ملغي"

    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="purchase_orders")
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name="purchase_orders")
    warehouse = models.ForeignKey(Warehouse, on_delete=models.PROTECT, related_name="purchase_orders")
    order_number = models.CharField(max_length=40)
    order_date = models.DateField()
    expected_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="created_purchase_orders")
    created_at = models.DateTimeField(auto_now_add=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-order_date", "-id")
        constraints = [models.UniqueConstraint(fields=("company", "order_number"), name="uq_purchase_order_company_no")]
        indexes = [models.Index(fields=("company", "status", "order_date"), name="pur_order_co_status_date_idx")]

    @property
    def company_id_for_permission(self):
        return self.company_id

    def clean(self):
        super().clean()
        if self.supplier_id and self.supplier.company_id != self.company_id:
            raise ValidationError({"supplier": "المورد لا يتبع الشركة المحددة."})
        if self.warehouse_id and self.warehouse.company_id != self.company_id:
            raise ValidationError({"warehouse": "المخزن لا يتبع الشركة المحددة."})

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk).exclude(status=self.Status.DRAFT).exists():
            raise ValidationError("أمر الشراء المعتمد غير قابل للتعديل.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.status != self.Status.DRAFT:
            raise ValidationError("لا يمكن حذف أمر شراء معتمد أو مرتبط بحركات.")
        return super().delete(*args, **kwargs)


class PurchaseOrderLine(models.Model):
    """يمثل صنفًا وكمية وسعرًا داخل أمر الشراء."""
    order = models.ForeignKey(PurchaseOrder, on_delete=models.PROTECT, related_name="lines")
    product = models.ForeignKey("inventory.Product", on_delete=models.PROTECT, related_name="purchase_order_lines")
    quantity = models.DecimalField(max_digits=18, decimal_places=4, validators=[MinValueValidator(Decimal("0.0001"))])
    unit_price = models.DecimalField(max_digits=18, decimal_places=4, validators=[MinValueValidator(Decimal("0"))])
    tax_rate = models.DecimalField(max_digits=7, decimal_places=4, default=0, validators=[MinValueValidator(0), MaxValueValidator(100)])
    received_quantity = models.DecimalField(max_digits=18, decimal_places=4, default=0)

    class Meta:
        ordering = ("id",)

    @property
    def remaining_quantity(self):
        return self.quantity - self.received_quantity

    def clean(self):
        super().clean()
        if self.product_id and (self.product.company_id != self.order.company_id or self.product.product_type != "goods"):
            raise ValidationError({"product": "اختر صنفًا مخزنيًا تابعًا لشركة أمر الشراء."})
        if self.received_quantity > self.quantity:
            raise ValidationError({"received_quantity": "الكمية المستلمة تتجاوز كمية أمر الشراء."})

    def save(self, *args, **kwargs):
        if self.order_id and self.order.status != PurchaseOrder.Status.DRAFT:
            raise ValidationError("لا يمكن تعديل بنود أمر شراء معتمد.")
        return super().save(*args, **kwargs)


class PurchaseOrderReceiptLine(models.Model):
    """يربط كميات الاستلام ببنود أمر الشراء لتتبع التنفيذ."""
    receipt_line = models.OneToOneField(StockMovementLine, on_delete=models.PROTECT, related_name="purchase_order_link")
    order_line = models.ForeignKey(PurchaseOrderLine, on_delete=models.PROTECT, related_name="receipt_links")
    quantity = models.DecimalField(max_digits=18, decimal_places=4, validators=[MinValueValidator(Decimal("0.0001"))])

    class Meta:
        ordering = ("id",)
