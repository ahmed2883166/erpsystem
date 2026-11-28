"""نماذج دورة المشتريات من أوامر الشراء والاستلام إلى فواتير الموردين والمدفوعات."""

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, MaxValueValidator
from django.db import models

from accounting.models import ChartOfAccount, CostCenter, JournalEntry, TaxCode
from core.money import round_currency_amount
from companies.models import Branch, Company, CompanyActivity, Currency, ExchangeRate
from inventory.models import StockMovement, StockMovementLine, Warehouse
from suppliers.models import Supplier


class PurchaseReceipt(models.Model):
    """يمثل مستند استلام مشتريات المرتبط بحركة المخزون."""
    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="purchase_receipts")
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name="purchase_receipts")
    purchase_order = models.ForeignKey("PurchaseOrder", null=True, blank=True, on_delete=models.PROTECT, related_name="receipts")
    warehouse = models.ForeignKey(Warehouse, on_delete=models.PROTECT, related_name="purchase_receipts")
    stock_movement = models.OneToOneField(StockMovement, on_delete=models.PROTECT, related_name="purchase_receipt")
    journal_entry = models.OneToOneField(JournalEntry, null=True, blank=True, on_delete=models.PROTECT, related_name="purchase_receipt")
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
        if self.activity_id and (
            self.activity.company_id != self.company_id or not self.activity.is_active
        ):
            raise ValidationError({"activity": "نشاط الفاتورة يجب أن يكون مفعّلًا ويتبع الشركة المحددة."})
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
    class InvoiceType(models.TextChoices):
        INVENTORY = "inventory", "مشتريات مخزنية"
        DIRECT_EXPENSE = "direct_expense", "خدمات ومصروفات مباشرة"

    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name="purchase_invoices")
    activity = models.ForeignKey(
        CompanyActivity, null=True, blank=True, on_delete=models.PROTECT,
        related_name="purchase_invoices",
    )
    invoice_type = models.CharField(max_length=20, choices=InvoiceType.choices, default=InvoiceType.INVENTORY)
    service_acceptance = models.OneToOneField(
        "PurchaseServiceAcceptance", null=True, blank=True, on_delete=models.PROTECT,
        related_name="supplier_invoice",
    )
    receipt = models.ForeignKey(PurchaseReceipt, null=True, blank=True, on_delete=models.PROTECT, related_name="invoices")
    branch = models.ForeignKey(Branch, null=True, blank=True, on_delete=models.PROTECT, related_name="purchase_invoices")
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
        if self.receipt_id and (
            self.receipt.company_id != self.company_id or self.receipt.supplier_id != self.supplier_id
        ):
            raise ValidationError({"receipt": "إذن الاستلام لا يتبع المورد والشركة المحددين."})
        if self.branch_id and self.branch.company_id != self.company_id:
            raise ValidationError({"branch": "الفرع لا يتبع الشركة المحددة."})
        if self.service_acceptance_id:
            acceptance = self.service_acceptance
            if self.invoice_type != self.InvoiceType.DIRECT_EXPENSE:
                raise ValidationError({"service_acceptance": "محضر إنجاز الخدمة يرتبط بفاتورة خدمات ومصروفات مباشرة فقط."})
            if acceptance.status != PurchaseServiceAcceptance.Status.ACCEPTED:
                raise ValidationError({"service_acceptance": "لا يمكن ربط الفاتورة إلا بمحضر إنجاز خدمة معتمد."})
            if acceptance.company_id != self.company_id or acceptance.supplier_id != self.supplier_id:
                raise ValidationError({"service_acceptance": "محضر الإنجاز لا يتبع نفس الشركة والمورد."})
            if acceptance.acceptance_date > self.invoice_date:
                raise ValidationError({"service_acceptance": "تاريخ الفاتورة لا يسبق تاريخ اعتماد إنجاز الخدمة."})
        if self.invoice_type == self.InvoiceType.INVENTORY and not self.receipt_id:
            raise ValidationError({"receipt": "فاتورة المشتريات المخزنية تتطلب إذن استلام."})
        if self.invoice_type == self.InvoiceType.INVENTORY and self.branch_id:
            raise ValidationError({"branch": "فرع فاتورة المخزون يُحدد من مخزن إذن الاستلام."})
        if self.invoice_type == self.InvoiceType.DIRECT_EXPENSE and (self.receipt_id or not self.branch_id):
            raise ValidationError({"branch": "فاتورة الخدمات والمصروفات المباشرة تتطلب فرعًا ولا ترتبط بإذن استلام."})
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
    receipt_line = models.ForeignKey(StockMovementLine, null=True, blank=True, on_delete=models.PROTECT, related_name="purchase_invoice_lines")
    description = models.CharField(max_length=240, blank=True)
    expense_account = models.ForeignKey(ChartOfAccount, null=True, blank=True, on_delete=models.PROTECT, related_name="purchase_expense_lines")
    cost_center = models.ForeignKey(CostCenter, null=True, blank=True, on_delete=models.PROTECT, related_name="purchase_invoice_lines")
    billed_quantity = models.DecimalField(max_digits=18, decimal_places=4, validators=[MinValueValidator(Decimal("0.0001"))])
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
        constraints = [
            models.UniqueConstraint(fields=("invoice", "receipt_line"), name="uq_purchase_invoice_receipt_line"),
            models.CheckConstraint(condition=models.Q(billed_quantity__gt=0), name="ck_purchase_invoice_line_qty_positive"),
        ]

    def calculate(self):
        if self.tax_code_id:
            code = self.tax_code
            if (not code.is_active or code.company_id != self.invoice.company_id
                    or code.country != self.invoice.company.country
                    or code.category == TaxCode.Category.REVERSE_CHARGE
                    or code.effective_from > self.invoice.invoice_date
                    or (code.effective_to and code.effective_to < self.invoice.invoice_date)):
                raise ValidationError({"tax_code": "كود الضريبة غير صالح أو غير قابل للخصم لهذه الشركة أو تاريخ الفاتورة."})
            self.tax_rate = code.rate
        quantity = self.billed_quantity
        if self.invoice.transaction_currency_id:
            if self.transaction_unit_price is None:
                raise ValidationError({"transaction_unit_price": "سعر الوحدة بالعملة الأجنبية مطلوب."})
            self.transaction_net_amount = (quantity * self.transaction_unit_price).quantize(Decimal("0.0001"))
            self.transaction_tax_amount = round_currency_amount(self.transaction_net_amount * self.tax_rate / Decimal("100"), self.invoice.transaction_currency)
            self.transaction_total_amount = self.transaction_net_amount + self.transaction_tax_amount
            self.unit_price = (self.transaction_unit_price * self.invoice.exchange_rate).quantize(Decimal("0.0001"))
            self.net_amount = (self.transaction_net_amount * self.invoice.exchange_rate).quantize(Decimal("0.0001"))
            self.tax_amount = round_currency_amount(self.transaction_tax_amount * self.invoice.exchange_rate, self.invoice.company.base_currency)
            self.total_amount = self.net_amount + self.tax_amount
        else:
            if self.transaction_unit_price is not None:
                raise ValidationError({"transaction_unit_price": "لا تستخدم سعر وحدة أجنبيًا مع فاتورة بالعملة الأساسية."})
            self.transaction_net_amount = self.transaction_tax_amount = self.transaction_total_amount = None
            self.net_amount = (quantity * self.unit_price).quantize(Decimal("0.0001"))
            self.tax_amount = round_currency_amount(self.net_amount * self.tax_rate / Decimal("100"), self.invoice.company.base_currency)
            self.total_amount = self.net_amount + self.tax_amount

    def clean(self):
        super().clean()
        if not self.invoice_id:
            return
        if self.invoice.invoice_type == PurchaseInvoice.InvoiceType.INVENTORY:
            if not self.receipt_line_id or not self.invoice.receipt_id:
                raise ValidationError({"receipt_line": "فاتورة المشتريات المخزنية تتطلب سطر استلام."})
            if self.receipt_line.movement_id != self.invoice.receipt.stock_movement_id:
                raise ValidationError({"receipt_line": "سطر الصنف لا يتبع إذن الاستلام المحدد."})
            if self.receipt_line.product.product_type != "goods":
                raise ValidationError({"receipt_line": "لا يمكن فوترة خدمة من خلال استلام مخزني."})
            if self.expense_account_id or self.cost_center_id:
                raise ValidationError({"expense_account": "لا تستخدم حساب مصروف أو مركز تكلفة في سطر مخزني."})
        else:
            if self.receipt_line_id:
                raise ValidationError({"receipt_line": "الفاتورة المباشرة لا ترتبط بسطر استلام مخزني."})
            if not self.expense_account_id:
                raise ValidationError({"expense_account": "حدد حساب المصروف لكل بند."})
            account = self.expense_account
            if (account.company_id != self.invoice.company_id or account.nature != ChartOfAccount.Nature.EXPENSE or not account.is_active or account.is_group or not account.allow_posting):
                raise ValidationError({"expense_account": "اختر حساب مصروف نشطًا وتفصيليًا وقابلًا للترحيل من نفس الشركة."})
            if self.cost_center_id and (self.cost_center.company_id != self.invoice.company_id or not self.cost_center.is_active):
                raise ValidationError({"cost_center": "مركز التكلفة غير نشط أو لا يتبع الشركة."})
            if not self.description.strip():
                raise ValidationError({"description": "وصف بند الخدمة أو المصروف مطلوب."})

    def save(self, *args, **kwargs):
        if self.invoice_id and PurchaseInvoice.objects.filter(
            pk=self.invoice_id, status=PurchaseInvoice.Status.POSTED
        ).exists():
            raise ValidationError("لا يمكن تعديل سطور فاتورة مورد مرحلة.")
        if self.billed_quantity is None:
            self.billed_quantity = self.receipt_line.quantity if self.receipt_line_id else Decimal("1")
        self.calculate()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.invoice.status == PurchaseInvoice.Status.POSTED:
            raise ValidationError("لا يمكن حذف سطور فاتورة مورد مرحلة.")
        return super().delete(*args, **kwargs)


class PurchaseInvoiceTaxAllocation(models.Model):
    """يحفظ توزيع ضريبة المدخلات غير القابلة للخصم بين المخزون والتكلفة المستهلكة حسب الفرع."""
    invoice = models.ForeignKey(
        PurchaseInvoice, on_delete=models.PROTECT, related_name="tax_allocations",
    )
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name="purchase_tax_allocations")
    journal_entry = models.OneToOneField(
        "accounting.JournalEntry", on_delete=models.PROTECT,
        related_name="purchase_invoice_tax_allocation",
    )
    inventory_amount = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    cogs_amount = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    adjustment_loss_amount = models.DecimalField(max_digits=20, decimal_places=4, default=0)

    class Meta:
        ordering = ("branch_id", "id")
        constraints = [
            models.UniqueConstraint(fields=("invoice", "branch"), name="uq_purchase_tax_allocation_branch"),
        ]


class SupplierPayment(models.Model):
    """يمثل دفعة مسددة للمورد وارتباطها بالحسابات."""
    class Method(models.TextChoices):
        CASH = "cash", "نقدي"
        BANK = "bank", "تحويل بنكي"

    class Status(models.TextChoices):
        DRAFT = "draft", "مسودة"
        POSTED = "posted", "مرحّل"
        REVERSED = "reversed", "معكوس"

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
    historical_carrying_base_amount = models.DecimalField(max_digits=20, decimal_places=4, null=True, blank=True)
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
        if self.pk and type(self).objects.filter(pk=self.pk, status__in=(self.Status.POSTED, self.Status.REVERSED)).exists():
            raise ValidationError("سند صرف المورد المرحّل غير قابل للتعديل.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.status in (self.Status.POSTED, self.Status.REVERSED):
            raise ValidationError("لا يمكن حذف سند صرف مورد مرحّل.")
        return super().delete(*args, **kwargs)


class SupplierRefund(models.Model):
    """استرداد نقدي من المورد مقابل رصيد دائن نشأ عن مرتجع مرحّل."""

    class Method(models.TextChoices):
        CASH = "cash", "نقدي"
        BANK = "bank", "تحويل بنكي"

    class Status(models.TextChoices):
        DRAFT = "draft", "مسودة"
        POSTED = "posted", "مرحّل"
        REVERSED = "reversed", "معكوس"

    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="supplier_refunds")
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name="refunds")
    return_document = models.ForeignKey(
        "SupplierReturn", on_delete=models.PROTECT, related_name="refunds",
    )
    refund_number = models.CharField(max_length=40)
    refund_date = models.DateField()
    method = models.CharField(max_length=8, choices=Method.choices, default=Method.BANK)
    payment_account = models.ForeignKey(
        ChartOfAccount, on_delete=models.PROTECT, related_name="supplier_refunds",
    )
    amount = models.DecimalField(
        max_digits=20, decimal_places=4, validators=[MinValueValidator(Decimal("0.0001"))],
    )
    reference = models.CharField(max_length=120, blank=True)
    notes = models.TextField(blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    journal_entry = models.OneToOneField(
        "accounting.JournalEntry", null=True, blank=True, on_delete=models.PROTECT,
        related_name="supplier_refund",
    )
    reversal_journal_entry = models.OneToOneField(
        "accounting.JournalEntry", null=True, blank=True, on_delete=models.PROTECT,
        related_name="supplier_refund_reversal",
    )
    reversal_date = models.DateField(null=True, blank=True)
    reversed_at = models.DateTimeField(null=True, blank=True)
    reversed_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.PROTECT,
        related_name="reversed_supplier_refunds",
    )
    created_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.PROTECT,
        related_name="created_supplier_refunds",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    posted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-refund_date", "-id")
        constraints = [
            models.UniqueConstraint(fields=("company", "refund_number"), name="uq_supplier_refund_company_no"),
        ]
        indexes = [
            models.Index(fields=("company", "status", "refund_date"), name="pur_refund_co_status_idx"),
        ]

    @property
    def company_id_for_permission(self):
        return self.company_id

    def clean(self):
        super().clean()
        if self.supplier_id and self.supplier.company_id != self.company_id:
            raise ValidationError({"supplier": "المورد لا يتبع الشركة المحددة."})
        if self.return_document_id and (
            self.return_document.company_id != self.company_id
            or self.return_document.supplier_id != self.supplier_id
        ):
            raise ValidationError({"return_document": "مرتجع المورد لا يطابق الشركة أو المورد."})
        if self.return_document_id and self.return_document.status != SupplierReturn.Status.POSTED:
            raise ValidationError({"return_document": "لا يمكن استرداد مبلغ عن مرتجع غير مرحّل أو معكوس."})

    def save(self, *args, **kwargs):
        if self.pk:
            current_status = type(self).objects.filter(pk=self.pk).values_list("status", flat=True).first()
            if current_status == self.Status.REVERSED:
                raise ValidationError("استرداد المورد المعكوس غير قابل للتعديل.")
            if current_status == self.Status.POSTED:
                reversal_fields = {"reversal_journal_entry", "reversal_date", "reversed_at", "reversed_by", "status"}
                update_fields = set(kwargs.get("update_fields") or ())
                if self.status != self.Status.REVERSED or not update_fields or not update_fields.issubset(reversal_fields):
                    raise ValidationError("استرداد المورد المرحّل غير قابل للتعديل إلا من خلال إجراء العكس.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.status in (self.Status.POSTED, self.Status.REVERSED):
            raise ValidationError("لا يمكن حذف استرداد مورد مرحّل أو معكوس.")
        return super().delete(*args, **kwargs)


class PurchaseRFQ(models.Model):
    """طلب عروض أسعار من موردين مدعوين، مرتبط باحتياج داخلي اختياري."""
    class Status(models.TextChoices):
        DRAFT = "draft", "مسودة"
        OPEN = "open", "مفتوح لاستقبال العروض"
        CLOSED = "closed", "مغلق"
        AWARDED = "awarded", "تمت الترسية"
        CANCELLED = "cancelled", "ملغي"

    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="purchase_rfqs")
    source_requisition = models.ForeignKey("PurchaseRequisition", null=True, blank=True, on_delete=models.PROTECT, related_name="rfqs")
    rfq_number = models.CharField(max_length=40)
    issue_date = models.DateField()
    closing_date = models.DateField()
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT)
    terms = models.TextField(blank=True)
    created_by = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="created_purchase_rfqs")
    awarded_quotation = models.OneToOneField("SupplierQuotation", null=True, blank=True, on_delete=models.PROTECT, related_name="awarded_rfq")
    converted_order = models.OneToOneField("PurchaseOrder", null=True, blank=True, on_delete=models.PROTECT, related_name="source_rfq")
    award_reason = models.TextField(blank=True)
    awarded_by = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="awarded_purchase_rfqs")
    awarded_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-issue_date", "-id")
        constraints = [models.UniqueConstraint(fields=("company", "rfq_number"), name="uq_purchase_rfq_company_no")]
        indexes = [models.Index(fields=("company", "status", "closing_date"), name="pur_rfq_co_status_close_idx")]

    @property
    def company_id_for_permission(self):
        return self.company_id

    def clean(self):
        super().clean()
        if self.closing_date < self.issue_date:
            raise ValidationError({"closing_date": "تاريخ إغلاق طلب العروض لا يمكن أن يسبق تاريخ الإصدار."})
        if self.source_requisition_id and self.source_requisition.company_id != self.company_id:
            raise ValidationError({"source_requisition": "طلب الاحتياج لا يتبع الشركة المحددة."})


class PurchaseRFQLine(models.Model):
    rfq = models.ForeignKey(PurchaseRFQ, on_delete=models.PROTECT, related_name="lines")
    product = models.ForeignKey("inventory.Product", on_delete=models.PROTECT, related_name="purchase_rfq_lines")
    quantity = models.DecimalField(max_digits=18, decimal_places=4, validators=[MinValueValidator(Decimal("0.0001"))])
    notes = models.CharField(max_length=250, blank=True)

    class Meta:
        ordering = ("id",)
        constraints = [models.UniqueConstraint(fields=("rfq", "product"), name="uq_purchase_rfq_product")]

    def clean(self):
        super().clean()
        if self.product_id and (self.product.company_id != self.rfq.company_id or self.product.product_type not in ("goods", "service") or not self.product.is_active):
            raise ValidationError({"product": "اختر صنفًا أو خدمة نشطة تابعة لشركة طلب العروض."})


class PurchaseRFQInvitation(models.Model):
    class Status(models.TextChoices):
        INVITED = "invited", "مدعو"
        DECLINED = "declined", "اعتذر"
        QUOTED = "quoted", "قدم عرضًا"

    rfq = models.ForeignKey(PurchaseRFQ, on_delete=models.PROTECT, related_name="invitations")
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name="purchase_rfq_invitations")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.INVITED)
    invited_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=("rfq", "supplier"), name="uq_purchase_rfq_supplier")]


class SupplierQuotation(models.Model):
    class Status(models.TextChoices):
        SUBMITTED = "submitted", "مقدم"
        WITHDRAWN = "withdrawn", "مسحوب"

    rfq = models.ForeignKey(PurchaseRFQ, on_delete=models.PROTECT, related_name="quotations")
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name="purchase_quotations")
    quotation_number = models.CharField(max_length=60)
    quotation_date = models.DateField()
    valid_until = models.DateField()
    delivery_days = models.PositiveSmallIntegerField(default=0)
    payment_terms = models.CharField(max_length=160, blank=True)
    notes = models.TextField(blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.SUBMITTED)
    submitted_by = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="submitted_supplier_quotations")
    submitted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("quotation_date", "id")
        constraints = [
            models.UniqueConstraint(fields=("rfq", "supplier"), name="uq_supplier_quote_rfq_supplier"),
            models.UniqueConstraint(fields=("supplier", "quotation_number"), name="uq_supplier_quote_supplier_no"),
        ]

    @property
    def company_id_for_permission(self):
        return self.rfq.company_id

    def clean(self):
        super().clean()
        if self.supplier_id and self.supplier.company_id != self.rfq.company_id:
            raise ValidationError({"supplier": "المورد لا يتبع شركة طلب العروض."})
        if self.valid_until < self.quotation_date:
            raise ValidationError({"valid_until": "صلاحية العرض لا يمكن أن تنتهي قبل تاريخ العرض."})
        if self.rfq_id and not self.rfq.invitations.filter(supplier_id=self.supplier_id).exists():
            raise ValidationError({"supplier": "لا يمكن تسجيل عرض من مورد غير مدعو."})


class SupplierQuotationLine(models.Model):
    quotation = models.ForeignKey(SupplierQuotation, on_delete=models.PROTECT, related_name="lines")
    rfq_line = models.ForeignKey(PurchaseRFQLine, on_delete=models.PROTECT, related_name="quotation_lines")
    unit_price = models.DecimalField(max_digits=18, decimal_places=4, validators=[MinValueValidator(Decimal("0"))])
    discount_percent = models.DecimalField(max_digits=7, decimal_places=4, default=0, validators=[MinValueValidator(Decimal("0")), MaxValueValidator(100)])
    tax_rate = models.DecimalField(max_digits=7, decimal_places=4, default=0, validators=[MinValueValidator(Decimal("0")), MaxValueValidator(100)])

    class Meta:
        ordering = ("id",)
        constraints = [models.UniqueConstraint(fields=("quotation", "rfq_line"), name="uq_supplier_quote_line")]

    def clean(self):
        super().clean()
        if self.rfq_line_id and self.quotation_id and self.rfq_line.rfq_id != self.quotation.rfq_id:
            raise ValidationError({"rfq_line": "بند العرض لا يتبع طلب العروض المحدد."})

    @property
    def net_amount(self):
        return (self.rfq_line.quantity * self.unit_price * (Decimal("1") - self.discount_percent / Decimal("100"))).quantize(Decimal("0.0001"))

    @property
    def tax_amount(self):
        return (self.net_amount * self.tax_rate / Decimal("100")).quantize(Decimal("0.0001"))

    @property
    def total_amount(self):
        return self.net_amount + self.tax_amount


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


    @property
    def grand_total(self):
        return sum(
            (line.quantity * line.unit_price * (Decimal("1") + line.tax_rate / Decimal("100")) for line in self.lines.all()),
            Decimal("0"),
        ).quantize(Decimal("0.0001"))

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
        if self.product_id and (self.product.company_id != self.order.company_id or self.product.product_type not in ("goods", "service") or not self.product.is_active):
            raise ValidationError({"product": "اختر صنفًا أو خدمة نشطة تابعة لشركة أمر الشراء."})
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


class SupplierReturn(models.Model):
    """إشعار مرتجع مورد مرتبط بفاتورة الشراء وحركة مخزون مصدرية."""

    class Status(models.TextChoices):
        DRAFT = "draft", "مسودة"
        POSTED = "posted", "مرحّل"
        REVERSED = "reversed", "معكوس"

    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="supplier_returns")
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name="returns")
    invoice = models.ForeignKey(PurchaseInvoice, on_delete=models.PROTECT, related_name="returns")
    stock_movement = models.OneToOneField(
        StockMovement, on_delete=models.PROTECT, related_name="supplier_return"
    )
    return_number = models.CharField(max_length=40)
    return_date = models.DateField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    net_amount = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    tax_amount = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    grand_total = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    transaction_net_amount = models.DecimalField(max_digits=20, decimal_places=4, null=True, blank=True)
    transaction_tax_amount = models.DecimalField(max_digits=20, decimal_places=4, null=True, blank=True)
    transaction_grand_total = models.DecimalField(max_digits=20, decimal_places=4, null=True, blank=True)
    journal_entry = models.OneToOneField(
        "accounting.JournalEntry", null=True, blank=True, on_delete=models.PROTECT,
        related_name="supplier_return",
    )
    reversal_stock_movement = models.OneToOneField(
        StockMovement, null=True, blank=True, on_delete=models.PROTECT,
        related_name="supplier_return_reversal",
    )
    reversal_journal_entry = models.OneToOneField(
        "accounting.JournalEntry", null=True, blank=True, on_delete=models.PROTECT,
        related_name="supplier_return_reversal",
    )
    reversal_date = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.PROTECT,
        related_name="created_supplier_returns",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    posted_at = models.DateTimeField(null=True, blank=True)
    reversed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-return_date", "-id")
        constraints = [
            models.UniqueConstraint(fields=("company", "return_number"), name="uq_supplier_return_company_no"),
        ]
        indexes = [
            models.Index(fields=("company", "status", "return_date"), name="pur_return_co_status_date_idx"),
        ]

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
            raise ValidationError({"invoice": "فاتورة المورد لا تطابق الشركة أو المورد."})
        if self.stock_movement_id and (
            self.stock_movement.company_id != self.company_id
            or self.stock_movement.movement_type != StockMovement.MovementType.SUPPLIER_RETURN
        ):
            raise ValidationError({"stock_movement": "حركة المخزون ليست مرتجع مورد تابعًا للشركة."})
        if self.reversal_stock_movement_id and (
            self.reversal_stock_movement.company_id != self.company_id
            or self.reversal_stock_movement.movement_type != StockMovement.MovementType.SUPPLIER_RETURN_REVERSAL
        ):
            raise ValidationError({"reversal_stock_movement": "حركة عكس المخزون غير صالحة."})

    def save(self, *args, **kwargs):
        if self.pk:
            old_status = type(self).objects.filter(pk=self.pk).values_list("status", flat=True).first()
            if old_status == self.Status.POSTED and self.status != self.Status.REVERSED:
                raise ValidationError("مرتجع المورد المرحّل غير قابل للتعديل إلا من خلال إجراء العكس.")
            if old_status == self.Status.REVERSED:
                raise ValidationError("مرتجع المورد المعكوس غير قابل للتعديل.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.status in (self.Status.POSTED, self.Status.REVERSED):
            raise ValidationError("لا يمكن حذف مرتجع مورد مرحّل أو معكوس.")
        return super().delete(*args, **kwargs)


class SupplierReturnLine(models.Model):
    """يربط كمية المرتجع ببند فاتورة المورد وسطر حركة المخزون."""

    return_document = models.ForeignKey(
        SupplierReturn, on_delete=models.PROTECT, related_name="lines"
    )
    invoice_line = models.ForeignKey(
        PurchaseInvoiceLine, on_delete=models.PROTECT, related_name="supplier_return_lines"
    )
    movement_line = models.OneToOneField(
        StockMovementLine, on_delete=models.PROTECT, related_name="supplier_return_line"
    )
    reversal_movement_line = models.OneToOneField(
        StockMovementLine, null=True, blank=True, on_delete=models.PROTECT,
        related_name="supplier_return_reversal_line",
    )
    quantity = models.DecimalField(
        max_digits=18, decimal_places=4, validators=[MinValueValidator(Decimal("0.0001"))]
    )
    net_amount = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    tax_amount = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    recoverable_tax_amount = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    nonrecoverable_tax_amount = models.DecimalField(max_digits=20, decimal_places=4, default=0)

    class Meta:
        ordering = ("id",)
        constraints = [
            models.UniqueConstraint(
                fields=("return_document", "invoice_line"),
                name="uq_supplier_return_invoice_line",
            ),
        ]

    def clean(self):
        super().clean()
        if self.return_document_id and self.invoice_line_id:
            if self.invoice_line.invoice_id != self.return_document.invoice_id:
                raise ValidationError({"invoice_line": "بند الفاتورة لا يتبع فاتورة المرتجع."})
        if self.return_document_id and self.movement_line_id:
            if self.movement_line.movement_id != self.return_document.stock_movement_id:
                raise ValidationError({"movement_line": "سطر المخزون لا يتبع حركة المرتجع."})

    def save(self, *args, **kwargs):
        if self.return_document_id and self.return_document.status in (SupplierReturn.Status.POSTED, SupplierReturn.Status.REVERSED):
            raise ValidationError("لا يمكن تعديل بند مرتجع مورد مرحّل أو معكوس.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.return_document.status in (SupplierReturn.Status.POSTED, SupplierReturn.Status.REVERSED):
            raise ValidationError("لا يمكن حذف بند مرتجع مورد مرحّل أو معكوس.")
        return super().delete(*args, **kwargs)


class PurchaseRequisition(models.Model):
    """طلب احتياج داخلي يسبق اختيار المورد وإصدار أمر الشراء."""
    class Status(models.TextChoices):
        DRAFT = "draft", "مسودة"
        SUBMITTED = "submitted", "مرسل للاعتماد"
        APPROVED = "approved", "معتمد"
        REJECTED = "rejected", "مرفوض"
        CONVERTED = "converted", "تحول إلى أمر شراء"
        CANCELLED = "cancelled", "ملغي"

    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="purchase_requisitions")
    request_number = models.CharField(max_length=40)
    request_date = models.DateField()
    required_date = models.DateField(null=True, blank=True)
    department = models.CharField(max_length=120, blank=True)
    purpose = models.TextField(blank=True)
    notes = models.TextField(blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT)
    requested_by = models.ForeignKey("accounts.User", on_delete=models.PROTECT, related_name="purchase_requisitions")
    reviewed_by = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="reviewed_purchase_requisitions")
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_note = models.TextField(blank=True)
    converted_order = models.OneToOneField("PurchaseOrder", null=True, blank=True, on_delete=models.PROTECT, related_name="source_requisition")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-request_date", "-id")
        constraints = [models.UniqueConstraint(fields=("company", "request_number"), name="uq_purchase_req_company_no")]
        indexes = [models.Index(fields=("company", "status", "request_date"), name="pur_req_co_status_date_idx")]

    @property
    def company_id_for_permission(self):
        return self.company_id

    def clean(self):
        super().clean()
        if self.required_date and self.required_date < self.request_date:
            raise ValidationError({"required_date": "تاريخ الاحتياج لا يمكن أن يسبق تاريخ الطلب."})

    def save(self, *args, **kwargs):
        if self.pk:
            old = type(self).objects.filter(pk=self.pk).values_list("status", flat=True).first()
            if old not in (None, self.Status.DRAFT) and not (old == self.Status.REJECTED and self.status == self.Status.DRAFT):
                raise ValidationError("لا يمكن تعديل طلب الشراء إلا إذا كان مسودة أو مرفوضًا ويعاد فتحه كمسودة.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.status != self.Status.DRAFT:
            raise ValidationError("لا يمكن حذف طلب شراء تم إرساله أو اعتماده.")
        return super().delete(*args, **kwargs)


class PurchaseRequisitionLine(models.Model):
    requisition = models.ForeignKey(PurchaseRequisition, on_delete=models.PROTECT, related_name="lines")
    product = models.ForeignKey("inventory.Product", on_delete=models.PROTECT, related_name="purchase_requisition_lines")
    requested_quantity = models.DecimalField(max_digits=18, decimal_places=4, validators=[MinValueValidator(Decimal("0.0001"))])
    estimated_unit_price = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True, validators=[MinValueValidator(Decimal("0"))])
    notes = models.CharField(max_length=250, blank=True)

    class Meta:
        ordering = ("id",)

    def clean(self):
        super().clean()
        if self.product_id and (self.product.company_id != self.requisition.company_id or self.product.product_type not in ("goods", "service") or not self.product.is_active):
            raise ValidationError({"product": "اختر صنفًا مخزنيًا نشطًا تابعًا لشركة الطلب."})

    def save(self, *args, **kwargs):
        if self.requisition_id and self.requisition.status != PurchaseRequisition.Status.DRAFT:
            raise ValidationError("لا يمكن تعديل بنود طلب شراء تم إرساله.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.requisition.status != PurchaseRequisition.Status.DRAFT:
            raise ValidationError("لا يمكن حذف بنود طلب شراء تم إرساله.")
        return super().delete(*args, **kwargs)


class PurchaseServiceAcceptance(models.Model):
    """محضر استلام/إنجاز خدمة لأمر شراء دون إنشاء حركة مخزون."""

    class Status(models.TextChoices):
        DRAFT = "draft", "مسودة"
        ACCEPTED = "accepted", "معتمد"
        REJECTED = "rejected", "مرفوض"

    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="purchase_service_acceptances")
    order = models.ForeignKey(PurchaseOrder, on_delete=models.PROTECT, related_name="service_acceptances")
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name="service_acceptances")
    acceptance_number = models.CharField(max_length=40)
    acceptance_date = models.DateField()
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT)
    notes = models.TextField(blank=True)
    accepted_by = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="accepted_purchase_services")
    accepted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-acceptance_date", "-id")
        constraints = [
            models.UniqueConstraint(fields=("company", "acceptance_number"), name="uq_purchase_service_acceptance_no"),
        ]
        indexes = [
            models.Index(fields=("company", "status", "acceptance_date"), name="pur_svc_accept_co_date_idx"),
        ]

    @property
    def company_id_for_permission(self):
        return self.company_id

    def clean(self):
        super().clean()
        if self.order_id and (
            self.order.company_id != self.company_id
            or self.order.supplier_id != self.supplier_id
            or self.order.status not in (PurchaseOrder.Status.CONFIRMED, PurchaseOrder.Status.PARTIALLY_RECEIVED, PurchaseOrder.Status.RECEIVED)
        ):
            raise ValidationError({"order": "اختر أمر شراء معتمدًا تابعًا لنفس الشركة والمورد."})

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk).exclude(status=self.Status.DRAFT).exists():
            raise ValidationError("محضر استلام الخدمة بعد اعتماده غير قابل للتعديل.")
        return super().save(*args, **kwargs)


class PurchaseServiceAcceptanceLine(models.Model):
    acceptance = models.ForeignKey(PurchaseServiceAcceptance, on_delete=models.PROTECT, related_name="lines")
    order_line = models.ForeignKey(PurchaseOrderLine, on_delete=models.PROTECT, related_name="service_acceptance_lines")
    quantity = models.DecimalField(max_digits=18, decimal_places=4, validators=[MinValueValidator(Decimal("0.0001"))])
    notes = models.CharField(max_length=250, blank=True)

    class Meta:
        ordering = ("id",)
        constraints = [
            models.UniqueConstraint(fields=("acceptance", "order_line"), name="uq_purchase_service_accept_line"),
        ]

    def clean(self):
        super().clean()
        if not self.order_line_id or not self.acceptance_id:
            return
        if (
            self.order_line.order_id != self.acceptance.order_id
            or self.order_line.product.product_type != "service"
        ):
            raise ValidationError({"order_line": "اختر بند خدمة تابعًا لأمر الشراء المحدد."})
        accepted_before = type(self).objects.filter(
            order_line_id=self.order_line_id,
            acceptance__status=PurchaseServiceAcceptance.Status.ACCEPTED,
        ).exclude(acceptance_id=self.acceptance_id).aggregate(total=models.Sum("quantity"))["total"] or Decimal("0")
        if accepted_before + self.quantity > self.order_line.quantity:
            raise ValidationError({"quantity": "كمية الخدمة المعتمدة تتجاوز كمية أمر الشراء المتبقية."})


class LandedCostDocument(models.Model):
    """مستند رسملة تكاليف الشراء الإضافية دون إنشاء مديونية مورد جديدة."""

    class Status(models.TextChoices):
        DRAFT = "draft", "مسودة"
        POSTED = "posted", "مرحّل"
        REVERSED = "reversed", "معكوس"

    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="landed_cost_documents")
    source_invoice = models.ForeignKey(
        PurchaseInvoice, on_delete=models.PROTECT, related_name="landed_cost_documents",
    )
    document_number = models.CharField(max_length=40)
    document_date = models.DateField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    total_amount = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.PROTECT,
        related_name="created_landed_cost_documents",
    )
    posted_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.PROTECT,
        related_name="posted_landed_cost_documents",
    )
    posted_at = models.DateTimeField(null=True, blank=True)
    reversal_journal_entry = models.ForeignKey(
        "accounting.JournalEntry", null=True, blank=True, on_delete=models.PROTECT,
        related_name="landed_cost_document_reversals",
    )
    reversed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-document_date", "-id")
        constraints = [
            models.UniqueConstraint(fields=("company", "document_number"), name="uq_landed_cost_company_no"),
            models.CheckConstraint(condition=models.Q(total_amount__gte=0), name="ck_landed_cost_total_nonnegative"),
        ]
        indexes = [
            models.Index(fields=("company", "status", "document_date"), name="pur_lcost_status_date_idx"),
        ]

    @property
    def company_id_for_permission(self):
        return self.company_id

    def clean(self):
        super().clean()
        if self.source_invoice_id and (
            self.source_invoice.company_id != self.company_id
            or self.source_invoice.invoice_type != PurchaseInvoice.InvoiceType.DIRECT_EXPENSE
            or self.source_invoice.status != PurchaseInvoice.Status.POSTED
        ):
            raise ValidationError({
                "source_invoice": "يجب اختيار فاتورة مصروفات مباشرة مرحلة وتتبع نفس الشركة."
            })

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(
            pk=self.pk,
        ).exclude(status=self.Status.DRAFT).exists():
            raise ValidationError("مستند رسملة التكلفة بعد ترحيله غير قابل للتعديل.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.status != self.Status.DRAFT:
            raise ValidationError("لا يمكن حذف مستند رسملة تكلفة مرحّل أو معكوس.")
        return super().delete(*args, **kwargs)


class LandedCostAllocation(models.Model):
    """يربط بند مصروف مورد ببند استلام مخزني محدد."""

    class Basis(models.TextChoices):
        VALUE = "value", "القيمة"
        QUANTITY = "quantity", "الكمية"
        WEIGHT = "weight", "الوزن"
        VOLUME = "volume", "الحجم"
        MANUAL = "manual", "توزيع يدوي"

    document = models.ForeignKey(
        LandedCostDocument, on_delete=models.PROTECT, related_name="allocations",
    )
    source_invoice_line = models.ForeignKey(
        PurchaseInvoiceLine, on_delete=models.PROTECT, related_name="landed_cost_allocations",
    )
    receipt_line = models.ForeignKey(
        StockMovementLine, on_delete=models.PROTECT, related_name="landed_cost_allocations",
    )
    basis = models.CharField(max_length=12, choices=Basis.choices, default=Basis.MANUAL)
    amount = models.DecimalField(
        max_digits=20, decimal_places=4, validators=[MinValueValidator(Decimal("0.0001"))],
    )
    source_journal_entry = models.OneToOneField(
        "accounting.JournalEntry", null=True, blank=True, on_delete=models.PROTECT,
        related_name="landed_cost_source_allocation",
    )
    source_reversal_journal_entry = models.OneToOneField(
        "accounting.JournalEntry", null=True, blank=True, on_delete=models.PROTECT,
        related_name="landed_cost_source_allocation_reversal",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("id",)
        constraints = [
            models.UniqueConstraint(
                fields=("document", "source_invoice_line", "receipt_line"),
                name="uq_lcost_doc_expense_receipt",
            ),
            models.CheckConstraint(condition=models.Q(amount__gt=0), name="ck_lcost_allocation_positive"),
        ]

    def clean(self):
        super().clean()
        if not self.document_id or not self.source_invoice_line_id or not self.receipt_line_id:
            return
        source = self.source_invoice_line
        receipt = self.receipt_line
        if source.invoice_id != self.document.source_invoice_id:
            raise ValidationError({"source_invoice_line": "بند المصروف لا يتبع فاتورة المصدر المحددة."})
        if source.invoice.invoice_type != PurchaseInvoice.InvoiceType.DIRECT_EXPENSE or not source.expense_account_id:
            raise ValidationError({"source_invoice_line": "بند المصدر يجب أن يكون بند مصروف مباشر بحساب مصروف."})
        account = source.expense_account
        if (
            not account.is_active or account.is_group or not account.allow_posting
            or account.nature != "expense"
        ):
            raise ValidationError({"source_invoice_line": "حساب المصروف يجب أن يكون تفصيليًا ونشطًا ويسمح بالترحيل."})
        if receipt.movement.company_id != self.document.company_id:
            raise ValidationError({"receipt_line": "بند الاستلام لا يتبع شركة مستند الرسملة."})
        if receipt.movement.status != StockMovement.Status.POSTED or receipt.product.product_type != "goods":
            raise ValidationError({"receipt_line": "اختر بند استلام مخزني مرحّل لصنف مخزني."})

    def save(self, *args, **kwargs):
        if self.document_id and self.document.status != LandedCostDocument.Status.DRAFT:
            raise ValidationError("لا يمكن تعديل توزيع مستند رسملة مرحّل.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.document.status != LandedCostDocument.Status.DRAFT:
            raise ValidationError("لا يمكن حذف توزيع تابع لمستند رسملة مرحّل.")
        return super().delete(*args, **kwargs)


class LandedCostDistribution(models.Model):
    """يحفظ نصيب الفرع من الرسملة مفصولًا بين المخزون والمبيعات والخسائر."""

    allocation = models.ForeignKey(
        LandedCostAllocation, on_delete=models.PROTECT, related_name="distributions",
    )
    branch = models.ForeignKey(
        Branch, on_delete=models.PROTECT, related_name="landed_cost_distributions",
    )
    inventory_amount = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    cogs_amount = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    adjustment_loss_amount = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    journal_entry = models.OneToOneField(
        "accounting.JournalEntry", null=True, blank=True, on_delete=models.PROTECT,
        related_name="landed_cost_distribution",
    )
    reversal_journal_entry = models.OneToOneField(
        "accounting.JournalEntry", null=True, blank=True, on_delete=models.PROTECT,
        related_name="landed_cost_distribution_reversal",
    )

    class Meta:
        ordering = ("branch_id", "id")
        constraints = [
            models.UniqueConstraint(fields=("allocation", "branch"), name="uq_lcost_allocation_branch"),
            models.CheckConstraint(condition=models.Q(inventory_amount__gte=0), name="ck_lcost_dist_inventory_nonnegative"),
            models.CheckConstraint(condition=models.Q(cogs_amount__gte=0), name="ck_lcost_dist_cogs_nonnegative"),
            models.CheckConstraint(condition=models.Q(adjustment_loss_amount__gte=0), name="ck_lcost_dist_loss_nonnegative"),
        ]

    def clean(self):
        super().clean()
        if self.allocation_id and self.branch_id and self.branch.company_id != self.allocation.document.company_id:
            raise ValidationError({"branch": "فرع التوزيع لا يتبع شركة مستند الرسملة."})
        if self.allocation_id and (
            self.allocation.document.status != LandedCostDocument.Status.DRAFT
            and not self.journal_entry_id
        ):
            raise ValidationError("لا يمكن تعديل توزيع مرحّل.")

    def save(self, *args, **kwargs):
        if self.allocation_id and self.allocation.document.status != LandedCostDocument.Status.DRAFT:
            raise ValidationError("توزيعات مستند الرسملة المرحّل غير قابلة للتعديل.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.allocation.document.status != LandedCostDocument.Status.DRAFT:
            raise ValidationError("لا يمكن حذف توزيع تابع لمستند رسملة مرحّل.")
        return super().delete(*args, **kwargs)


class BranchPurchasePolicy(models.Model):
    """Controls whether a branch may buy and which suppliers it may use."""
    branch = models.OneToOneField(
        Branch, on_delete=models.CASCADE, related_name="purchase_policy",
    )
    purchase_enabled = models.BooleanField(default=True)
    restrict_suppliers = models.BooleanField(default=False)
    allowed_suppliers = models.ManyToManyField(
        Supplier, blank=True, related_name="branch_purchase_policies",
    )
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.PROTECT,
        related_name="updated_branch_purchase_policies",
    )

    class Meta:
        verbose_name = "سياسة شراء فرع"
        verbose_name_plural = "سياسات شراء الفروع"

    def clean(self):
        super().clean()
        if self.branch_id and self.branch.company_id != self.branch.company_id:
            raise ValidationError({"branch": "الفرع غير صالح."})

    def __str__(self):
        return f"{self.branch} - {'مسموح' if self.purchase_enabled else 'موقوف'}"


class BranchPurchasePolicyAudit(models.Model):
    """Immutable audit record for branch purchase-policy changes."""
    policy = models.ForeignKey(
        BranchPurchasePolicy, on_delete=models.PROTECT, related_name="audit_entries",
    )
    changed_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.PROTECT,
        related_name="branch_purchase_policy_audits",
    )
    old_values = models.JSONField(default=dict)
    new_values = models.JSONField(default=dict)
    changed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-changed_at", "-pk")
        indexes = [models.Index(fields=("policy", "-changed_at"), name="branch_buy_audit_idx")]
