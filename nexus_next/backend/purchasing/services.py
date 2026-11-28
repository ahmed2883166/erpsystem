"""خدمات دورة المشتريات وترحيل الاستلام وفواتير الموردين ومدفوعاتهم."""

from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import F, Q, Sum
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from accounting.models import AccountRole, ChartOfAccount, FxSubledgerRevaluation, JournalEntry, JournalLine
from audit.models import AuditEvent
from accounting.services import get_account_for_role, post_journal, reverse_operational_journal
from companies.access import resolve_warehouse_branch
from companies.models import Branch, ExchangeRate, FiscalYear
from core.money import round_currency_amount
from inventory.models import Product, StockBalance, StockCostConsumption, StockCostLayer, StockMovement, StockMovementJournalLink, StockMovementLine, Warehouse
from inventory.services import post_stock_movement

from .models import LandedCostAllocation, LandedCostDistribution, LandedCostDocument, PurchaseInvoice, PurchaseInvoiceLine, PurchaseInvoiceTaxAllocation, PurchaseOrder, PurchaseOrderLine, PurchaseOrderReceiptLine, PurchaseReceipt, SupplierPayment, SupplierRefund, SupplierReturn, SupplierReturnLine


def _account(company, role, activity=None):
    """استخراج الحساب التفصيلي المرتبط بدور المشتريات والنشاط المحدد."""
    return get_account_for_role(company, role, activity=activity)


def _branch_and_year(receipt, entry_date):
    """تحديد فرع المخزن والسنة المالية لقيد فاتورة المشتريات."""
    branch = resolve_warehouse_branch(receipt.company, receipt.warehouse)
    if branch and not branch.is_active:
        raise ValidationError("فرع مخزن المشتريات موقوف.")
    if not branch:
        raise ValidationError("يجب إنشاء فرع نشط قبل ترحيل المشتريات.")
    year = FiscalYear.objects.filter(
        company=receipt.company, status=FiscalYear.Status.OPEN,
        start_date__lte=entry_date, end_date__gte=entry_date,
    ).order_by("-start_date").first()
    if not year:
        raise ValidationError("لا توجد سنة مالية مفتوحة تشمل تاريخ مستند المشتريات.")
    return branch, year


def _branch_and_year_for_invoice(invoice, entry_date):
    """Resolve branch and open fiscal year for either inventory or direct-expense invoices."""
    if invoice.invoice_type == PurchaseInvoice.InvoiceType.DIRECT_EXPENSE and invoice.branch_id:
        branch = invoice.branch
    elif invoice.receipt_id:
        branch = resolve_warehouse_branch(invoice.company, invoice.receipt.warehouse)
    else:
        branch = None
    if branch and (branch.company_id != invoice.company_id or not branch.is_active):
        raise ValidationError("فرع فاتورة المشتريات غير نشط أو لا يتبع الشركة.")
    if not branch:
        raise ValidationError("يجب تحديد فرع نشط قبل ترحيل مستند المورد.")
    year = FiscalYear.objects.filter(
        company=invoice.company, status=FiscalYear.Status.OPEN,
        start_date__lte=entry_date, end_date__gte=entry_date,
    ).order_by("-start_date").first()
    if not year:
        raise ValidationError("لا توجد سنة مالية مفتوحة تشمل تاريخ مستند المشتريات.")
    return branch, year


def _branch_for_stock_warehouse(company, warehouse):
    """تحديد الفرع الذي يحمل قيمة المخزون أو تكلفة الصرف."""
    branch = resolve_warehouse_branch(company, warehouse)
    if branch and not branch.is_active:
        raise ValidationError("لا يمكن توزيع تكلفة الضريبة على فرع مخزني موقوف.")
    if not branch:
        raise ValidationError("يجب إنشاء فرع نشط قبل توزيع تكلفة الضريبة غير القابلة للخصم.")
    return branch


def _capitalize_nonrecoverable_stock_tax(invoice_line):
    """يوزع الضريبة غير القابلة للخصم على المخزون المتبقي والتكلفة المستهلكة مع حفظ الأثر حسب الفرع."""
    receipt_line = invoice_line.receipt_line
    tax_amount = invoice_line.tax_amount
    quantity = receipt_line.quantity
    if tax_amount <= 0:
        return {}
    if quantity <= 0:
        raise ValidationError("لا يمكن توزيع ضريبة بند استلام كميته غير موجبة.")

    layers = list(StockCostLayer.objects.select_for_update(of=("self",)).filter(
        root_source_line=receipt_line,
    ).select_related("warehouse", "warehouse__branch"))
    if not layers:
        raise ValidationError(
            f"لا يمكن رسملة الضريبة للصنف {receipt_line.product.code}: "
            "لا توجد طبقات تكلفة مرتبطة باستلامه الأصلي. يلزم استلام جديد بعد تحديث تتبع التكلفة."
        )

    remaining_by_balance = defaultdict(Decimal)
    remaining_quantity = Decimal("0")
    for layer in layers:
        if layer.quantity_remaining <= 0:
            continue
        key = (layer.warehouse_id, layer.location_id, layer.expiry_date)
        remaining_by_balance[key] += layer.quantity_remaining
        remaining_quantity += layer.quantity_remaining

    consumed_by_branch = defaultdict(lambda: {"cogs": Decimal("0"), "loss": Decimal("0")})
    consumed_quantity = Decimal("0")
    consumptions = StockCostConsumption.objects.filter(
        source_layer__root_source_line=receipt_line,
        movement_line__movement__status=StockMovement.Status.POSTED,
    ).select_related(
        "movement_line__movement__source_warehouse",
        "movement_line__movement__source_warehouse__branch",
    )
    for consumption in consumptions:
        movement_line = consumption.movement_line
        movement = movement_line.movement
        if movement.movement_type in (
            StockMovement.MovementType.ISSUE,
            StockMovement.MovementType.SALES_RETURN_REVERSAL,
        ):
            category = "cogs"
        elif (
            movement.movement_type == StockMovement.MovementType.ADJUSTMENT
            and movement_line.adjustment_direction == "out"
        ):
            category = "loss"
        else:
            # التحويل يستهلك طبقة المصدر لكنه ينشئ طبقة ابنة؛ لذلك لا يعد استهلاكًا نهائيًا.
            continue
        branch = _branch_for_stock_warehouse(invoice_line.invoice.company, movement.source_warehouse)
        consumed_by_branch[branch.pk][category] += consumption.quantity
        consumed_quantity += consumption.quantity

    traced_quantity = remaining_quantity + consumed_quantity
    if abs(traced_quantity - quantity) > Decimal("0.0001"):
        raise ValidationError(
            f"تعذر مطابقة تتبع تكلفة الصنف {receipt_line.product.code}: "
            f"كمية الاستلام {quantity}، والمتبقي والمستهلك النهائي {traced_quantity}. "
            "لن يتم ترحيل ضريبة غير قابلة للخصم قبل اكتمال تتبع الكمية."
        )

    tax_per_unit = (tax_amount / quantity).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)
    buckets = []
    for (warehouse_id, location_id, expiry_date), remaining in remaining_by_balance.items():
        balance = StockBalance.objects.select_for_update().get(
            product_id=receipt_line.product_id,
            warehouse_id=warehouse_id,
            location_id=location_id,
            expiry_date=expiry_date,
        )
        branch = _branch_for_stock_warehouse(invoice_line.invoice.company, balance.warehouse)
        buckets.append({
            "kind": "inventory", "branch_id": branch.pk,
            "balance": balance, "raw": remaining * tax_per_unit,
        })

    for branch_id, categories in consumed_by_branch.items():
        for category, consumed in categories.items():
            if consumed > 0:
                buckets.append({
                    "kind": category, "branch_id": branch_id,
                    "balance": None, "raw": consumed * tax_per_unit,
                })

    if not buckets:
        raise ValidationError("لا توجد كميات متبقية أو مستهلكة يمكن توزيع الضريبة عليها.")

    rounded = [
        bucket["raw"].quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
        for bucket in buckets
    ]
    rounding_residual = tax_amount - sum(rounded, Decimal("0"))
    if rounding_residual:
        largest = max(range(len(buckets)), key=lambda index: buckets[index]["raw"])
        rounded[largest] += rounding_residual

    allocations = defaultdict(lambda: {
        "inventory": Decimal("0"), "cogs": Decimal("0"), "loss": Decimal("0"),
    })
    for bucket, amount in zip(buckets, rounded):
        if amount < 0:
            raise ValidationError("نتج توزيع ضريبي سالب؛ أوقف الترحيل لمراجعة تكلفة المخزون.")
        allocations[bucket["branch_id"]][bucket["kind"]] += amount
        if bucket["kind"] == "inventory" and amount:
            balance = bucket["balance"]
            balance.inventory_value += amount
            if balance.quantity > 0:
                balance.average_cost = (balance.inventory_value / balance.quantity).quantize(Decimal("0.000001"))
            balance.save(update_fields=("inventory_value", "average_cost", "updated_at"))

    for layer in layers:
        if layer.quantity_remaining > 0:
            layer.unit_cost = (layer.unit_cost + tax_per_unit).quantize(Decimal("0.00000001"))
            layer.save(update_fields=("unit_cost",))

    result = {}
    for branch_id, amounts in allocations.items():
        result[branch_id] = {
            "inventory": amounts["inventory"],
            "cogs": amounts["cogs"],
            "loss": amounts["loss"],
        }
    if sum(
        (a["inventory"] + a["cogs"] + a["loss"] for a in result.values()),
        Decimal("0"),
    ) != tax_amount:
        raise ValidationError("توزيع الضريبة غير متوازن؛ أوقف الترحيل لمراجعة الكميات.")
    return result


@transaction.atomic
def post_purchase_receipt(receipt_id, user):
    """ترحيل استلام المشتريات إلى المخزون وحساب البضاعة المستلمة غير المفوترة."""
    receipt = PurchaseReceipt.objects.select_for_update(of=("self",)).select_related(
        "company", "supplier", "warehouse", "stock_movement"
    ).get(pk=receipt_id)
    if receipt.stock_movement.status != StockMovement.Status.DRAFT:
        raise ValidationError("إذن الاستلام مرحّل بالفعل.")
    if receipt.supplier.company_id != receipt.company_id or receipt.warehouse.company_id != receipt.company_id:
        raise ValidationError("بيانات المورد أو المخزن لا تتبع الشركة.")
    if not receipt.supplier.is_active:
        raise ValidationError("المورد موقوف؛ لا يمكن ترحيل إذن استلام جديد.")
    if not receipt.warehouse.is_active:
        raise ValidationError("المخزن موقوف؛ لا يمكن ترحيل إذن استلام جديد.")
    post_stock_movement(receipt.stock_movement_id, user)
    if receipt.purchase_order_id:
        order = PurchaseOrder.objects.select_for_update().get(pk=receipt.purchase_order_id)
        if order.status not in (PurchaseOrder.Status.CONFIRMED, PurchaseOrder.Status.PARTIALLY_RECEIVED):
            raise ValidationError("أمر الشراء غير متاح للاستلام.")
        links = list(PurchaseOrderReceiptLine.objects.select_for_update().filter(
            receipt_line__movement_id=receipt.stock_movement_id
        ).select_related("order_line"))
        if not links:
            raise ValidationError("يجب ربط أصناف الاستلام ببنود أمر الشراء.")
        received_by_line = {}
        for link in links:
            line = link.order_line
            if line.order_id != order.pk or link.receipt_line.product_id != line.product_id:
                raise ValidationError("ربط صنف الاستلام ببند أمر الشراء غير صحيح.")
            received_by_line[line.pk] = received_by_line.get(line.pk, Decimal("0")) + link.quantity
        locked_lines = {
            line.pk: line for line in PurchaseOrderLine.objects.select_for_update().filter(
                pk__in=received_by_line, order=order
            )
        }
        if len(locked_lines) != len(received_by_line):
            raise ValidationError("بعض بنود أمر الشراء المرتبطة بالاستلام غير موجودة.")
        for line_id, quantity in received_by_line.items():
            line = locked_lines[line_id]
            if line.received_quantity + quantity > line.quantity:
                raise ValidationError("الكمية المستلمة تتجاوز كمية أمر الشراء.")
            PurchaseOrderLine.objects.filter(pk=line_id).update(received_quantity=line.received_quantity + quantity)
        remaining_exists = order.lines.filter(received_quantity__lt=F("quantity")).exists()
        PurchaseOrder.objects.filter(pk=order.pk).update(status=PurchaseOrder.Status.PARTIALLY_RECEIVED if remaining_exists else PurchaseOrder.Status.RECEIVED)

    # Inventory posting already creates the balanced Dr Inventory / Cr GRNI
    # journal and links it to this stock movement. Reuse that posted journal;
    # creating another purchase journal here would double the GRNI liability.
    movement_journals = list(
        StockMovementJournalLink.objects.filter(
            movement_id=receipt.stock_movement_id,
        ).select_related("journal_entry").order_by("id")
    )
    if len(movement_journals) != 1:
        raise ValidationError(
            "يجب أن ينتج عن ترحيل إذن الاستلام قيد مخزون واحد فقط؛ "
            "أوقفنا الترحيل لحماية حساب GRNI."
        )
    posted_entry = movement_journals[0].journal_entry
    if (
        posted_entry.status != JournalEntry.Status.POSTED
        or posted_entry.source_type != JournalEntry.Source.INVENTORY
        or posted_entry.company_id != receipt.company_id
    ):
        raise ValidationError("قيد المخزون المرتبط بإذن الاستلام غير صالح لإثبات GRNI.")
    PurchaseReceipt.objects.filter(pk=receipt.pk).update(journal_entry=posted_entry)

    return PurchaseReceipt.objects.select_related("stock_movement", "journal_entry").get(pk=receipt.pk)


def _validate_service_acceptance_invoice_amounts(invoice, lines):
    """Match a linked service invoice to the accepted quantities and PO value."""
    acceptance = invoice.service_acceptance
    if not acceptance:
        return
    acceptance = type(acceptance).objects.select_for_update().get(pk=acceptance.pk)
    if acceptance.status != "accepted":
        raise ValidationError("لا يمكن ترحيل فاتورة مرتبطة بمحضر خدمة غير معتمد.")
    acceptance_lines = list(
        acceptance.lines.select_related("order_line", "order_line__product")
        .select_for_update()
        .order_by("pk")
    )
    if not acceptance_lines:
        raise ValidationError("محضر إنجاز الخدمة المرتبط لا يحتوي على بنود معتمدة.")
    expected_net = Decimal("0")
    expected_tax = Decimal("0")
    expected_quantity = sum(
        (accepted_line.quantity for accepted_line in acceptance_lines),
        Decimal("0"),
    )
    actual_quantity = sum(
        (line.billed_quantity for line in lines),
        Decimal("0"),
    )
    if actual_quantity != expected_quantity:
        raise ValidationError(
            f"كمية فاتورة الخدمة ({actual_quantity}) لا تطابق الكمية المعتمدة بمحضر إنجاز الخدمة ({expected_quantity})."
        )
    for accepted_line in acceptance_lines:
        order_line = accepted_line.order_line
        if order_line.product.product_type != Product.ProductType.SERVICE:
            raise ValidationError("محضر الإنجاز يحتوي على بند غير خدمي.")
        if order_line.unit_price <= 0:
            raise ValidationError(
                "يجب تحديد سعر موجب لبنود الخدمة في أمر الشراء قبل مطابقة فاتورة المورد."
            )
        line_net = (accepted_line.quantity * order_line.unit_price).quantize(
            Decimal("0.0001"), rounding=ROUND_HALF_UP,
        )
        expected_net += line_net
        expected_tax += round_currency_amount(
            line_net * order_line.tax_rate / Decimal("100"),
            invoice.company.base_currency,
        )
    actual_net = sum((line.net_amount for line in lines), Decimal("0"))
    actual_tax = sum((line.tax_amount for line in lines), Decimal("0"))
    tolerance = Decimal("0.0002") * max(len(lines), len(acceptance_lines), 1)
    if abs(actual_net - expected_net) > tolerance:
        raise ValidationError(
            f"صافي فاتورة الخدمة ({actual_net}) لا يطابق قيمة الكميات المعتمدة بأمر الشراء ({expected_net})."
        )
    if abs(actual_tax - expected_tax) > tolerance:
        raise ValidationError(
            f"ضريبة فاتورة الخدمة ({actual_tax}) لا تطابق ضريبة بنود أمر الشراء المعتمدة ({expected_tax})."
        )


def _post_direct_expense_purchase_invoice(invoice, user):
    """Post a non-stock supplier invoice to expense accounts, input VAT and AP."""
    branch = invoice.branch
    if not branch or branch.company_id != invoice.company_id or not branch.is_active:
        raise ValidationError("حدد فرعًا نشطًا تابعًا للشركة قبل ترحيل الفاتورة.")
    year = FiscalYear.objects.filter(
        company=invoice.company, status=FiscalYear.Status.OPEN,
        start_date__lte=invoice.invoice_date, end_date__gte=invoice.invoice_date,
    ).order_by("-start_date").first()
    if not year:
        raise ValidationError("لا توجد سنة مالية مفتوحة تشمل تاريخ فاتورة المورد.")

    lines = list(invoice.lines.select_for_update(of=("self",)).select_related(
        "expense_account", "cost_center", "tax_code",
    ).order_by("pk"))
    if not lines:
        raise ValidationError("لا يمكن ترحيل فاتورة مورد بلا بنود.")
    net_amount = sum((line.net_amount for line in lines), Decimal("0")).quantize(Decimal("0.0001"))
    tax_amount = sum((line.tax_amount for line in lines), Decimal("0")).quantize(Decimal("0.0001"))
    recoverable_tax = sum(
        (line.tax_amount for line in lines if not line.tax_code_id or line.tax_code.input_tax_recoverable),
        Decimal("0"),
    ).quantize(Decimal("0.0001"))
    _validate_service_acceptance_invoice_amounts(invoice, lines)
    if net_amount <= 0 or tax_amount < 0:
        raise ValidationError("يجب أن تكون قيمة الفاتورة موجبة والضريبة غير سالبة.")

    for line in lines:
        account = line.expense_account
        if (not account or account.company_id != invoice.company_id
                or account.nature != ChartOfAccount.Nature.EXPENSE
                or not account.is_active or account.is_group or not account.allow_posting):
            raise ValidationError("يوجد حساب مصروف غير صالح في أحد بنود الفاتورة.")
        if line.cost_center_id and (
            line.cost_center.company_id != invoice.company_id or not line.cost_center.is_active
        ):
            raise ValidationError("يوجد مركز تكلفة غير صالح في أحد بنود الفاتورة.")

    if invoice.transaction_currency_id:
        from accounting.fx_arap import validate_snapshot
        approved = ExchangeRate.objects.filter(
            company=invoice.company, currency=invoice.transaction_currency,
            rate_date=invoice.exchange_rate_date, rate=invoice.exchange_rate,
        ).first()
        if not approved:
            raise ValidationError("سعر الصرف لا يطابق سعرًا معتمدًا بتاريخ فاتورة المورد.")
        foreign_net = sum((line.transaction_net_amount or Decimal("0") for line in lines), Decimal("0"))
        foreign_tax = sum((line.transaction_tax_amount or Decimal("0") for line in lines), Decimal("0"))
        foreign_total = foreign_net + foreign_tax
        try:
            expected_base = validate_snapshot(
                base_currency_code=invoice.company.base_currency.code,
                currency_code=invoice.transaction_currency.code,
                foreign_amount=foreign_total, rate=invoice.exchange_rate,
                rate_date=invoice.exchange_rate_date, document_date=invoice.invoice_date,
                approved_rate=approved.rate,
            )
        except ValueError:
            raise ValidationError("تعذر التحقق من سعر الصرف لفاتورة المورد.") from None
        if abs(expected_base - (net_amount + tax_amount)) > Decimal("0.0002") * max(len(lines), 1):
            raise ValidationError("إجمالي الفاتورة الأجنبية لا يطابق القيمة بالعملة الأساسية.")
        invoice.transaction_net_total = foreign_net
        invoice.transaction_tax_total = foreign_tax
        invoice.transaction_grand_total = foreign_total
    elif any(line.transaction_unit_price is not None for line in lines):
        raise ValidationError("توجد أسعار أجنبية دون تحديد عملة الفاتورة.")

    payable = _account(invoice.company, AccountRole.Role.ACCOUNTS_PAYABLE, invoice.activity)
    vat_input = _account(invoice.company, AccountRole.Role.VAT_INPUT, invoice.activity) if recoverable_tax else None
    journal_lines = []
    for line in lines:
        nonrecoverable_line_tax = (
            line.tax_amount
            if line.tax_code_id and not line.tax_code.input_tax_recoverable
            else Decimal("0")
        )
        debit_amount = (line.net_amount + nonrecoverable_line_tax).quantize(Decimal("0.0001"))
        if debit_amount:
            journal_lines.append(JournalLine(
                account=line.expense_account,
                description=line.description[:300],
                debit=debit_amount, credit=0, cost_center=line.cost_center,
            ))
    if recoverable_tax:
        if vat_input.pk == payable.pk:
            raise ValidationError("يجب فصل حساب ضريبة المدخلات عن حساب الموردين.")
        journal_lines.append(JournalLine(
            account=vat_input, description="ضريبة مدخلات قابلة للاسترداد",
            debit=recoverable_tax, credit=0,
        ))
    journal_lines.append(JournalLine(
        account=payable,
        description=f"إثبات مستحقات المورد {invoice.supplier.name_ar}",
        debit=0, credit=net_amount + tax_amount,
    ))
    if sum((line.debit for line in journal_lines), Decimal("0")) != sum(
        (line.credit for line in journal_lines), Decimal("0")
    ):
        raise ValidationError("قيد فاتورة المصروفات غير متوازن؛ راجع توزيع الضريبة.")

    entry = JournalEntry.objects.create(
        company=invoice.company, branch=branch, fiscal_year=year,
        entry_number=f"PUR-INV-{invoice.pk}", entry_date=invoice.invoice_date,
        description=f"فاتورة خدمات ومصروفات المورد {invoice.supplier.name_ar} رقم {invoice.invoice_number}"[:500],
        source_type=JournalEntry.Source.PURCHASES, created_by=user,
    )
    for line in journal_lines:
        line.entry = entry
    JournalLine.objects.bulk_create(journal_lines)
    try:
        posted_entry = post_journal(entry.pk, actor=user)
    except DjangoValidationError as exc:
        detail = exc.message_dict if hasattr(exc, "message_dict") else exc.messages
        raise ValidationError(detail) from exc

    invoice.net_total = net_amount
    invoice.tax_total = tax_amount
    invoice.grand_total = net_amount + tax_amount
    invoice.journal_entry = posted_entry
    invoice.status = PurchaseInvoice.Status.POSTED
    invoice.posted_at = timezone.now()
    invoice.save(update_fields=(
        "net_total", "tax_total", "grand_total", "transaction_net_total",
        "transaction_tax_total", "transaction_grand_total",
        "journal_entry", "status", "posted_at",
    ))
    return invoice


@transaction.atomic
def post_purchase_invoice(invoice_id, user):
    """ترحيل فاتورة المورد وتسوية GRNI والضريبة القابلة للاسترداد ورسملة الضريبة غير القابلة للخصم."""
    invoice = PurchaseInvoice.objects.select_for_update(of=("self",)).select_related(
        "company", "supplier", "receipt", "receipt__warehouse", "receipt__warehouse__branch",
    ).get(pk=invoice_id)
    if invoice.status != PurchaseInvoice.Status.DRAFT:
        raise ValidationError("فاتورة المورد مرحلة بالفعل.")
    from approvals.models import FinancialApprovalRequest
    from approvals.services import require_financial_approval
    try:
        require_financial_approval(FinancialApprovalRequest.DocumentType.PURCHASE_INVOICE, invoice)
    except DjangoValidationError as exc:
        raise ValidationError(exc.message_dict if hasattr(exc, "message_dict") else exc.messages) from exc
    if invoice.invoice_type == PurchaseInvoice.InvoiceType.DIRECT_EXPENSE:
        return _post_direct_expense_purchase_invoice(invoice, user)

    # Serialize postings per receipt. This prevents two concurrent invoices from
    # consuming the same remaining receipt quantity.
    receipt = PurchaseReceipt.objects.select_for_update().select_related(
        "company", "supplier", "warehouse", "stock_movement",
    ).get(pk=invoice.receipt_id)
    if receipt.stock_movement.status != StockMovement.Status.POSTED:
        raise ValidationError("لا يمكن ترحيل الفاتورة قبل ترحيل إذن الاستلام.")
    lines = list(invoice.lines.select_for_update(of=("self",)).select_related(
        "receipt_line", "receipt_line__product", "tax_code",
    ).order_by("receipt_line_id", "pk"))
    if not lines:
        raise ValidationError("لا يمكن ترحيل فاتورة مورد بلا بنود.")
    # Three-way match is quantity-aware: each invoice consumes only its billed
    # share of the posted receipt and the originating purchase-order line.
    tolerance = Decimal("0.0002")
    grni_amount = Decimal("0")
    for invoice_line in lines:
        receipt_line = StockMovementLine.objects.select_for_update(of=("self",)).select_related(
            "product", "movement",
        ).get(pk=invoice_line.receipt_line_id)
        if receipt_line.movement_id != receipt.stock_movement_id:
            raise ValidationError("سطر الفاتورة لا يتبع إذن الاستلام المحدد.")
        reserved_before = PurchaseInvoiceLine.objects.filter(
            receipt_line_id=receipt_line.pk,
            invoice__status__in=(PurchaseInvoice.Status.DRAFT, PurchaseInvoice.Status.POSTED),
        ).exclude(invoice_id=invoice.pk).aggregate(total=Sum("billed_quantity"))["total"] or Decimal("0")
        if reserved_before + invoice_line.billed_quantity > receipt_line.quantity:
            raise ValidationError(
                f"إجمالي الكميات المحجوزة بالفواتير يتجاوز الكمية المستلمة للصنف {receipt_line.product.code}."
            )
        prior_totals = PurchaseInvoiceLine.objects.filter(
            receipt_line_id=receipt_line.pk,
            invoice__status=PurchaseInvoice.Status.POSTED,
        ).exclude(invoice_id=invoice.pk).aggregate(
            quantity=Sum("billed_quantity"), net=Sum("net_amount"),
        )
        billed_before = prior_totals["quantity"] or Decimal("0")
        net_before = prior_totals["net"] or Decimal("0")
        billed_after = billed_before + invoice_line.billed_quantity
        if billed_after == receipt_line.quantity:
            # The final invoice consumes the exact residual, avoiding rounding
            # drift across multiple partial invoices.
            expected_net = (receipt_line.cost_amount - net_before).quantize(Decimal("0.0001"))
        else:
            expected_net = (
                receipt_line.cost_amount * invoice_line.billed_quantity / receipt_line.quantity
            ).quantize(Decimal("0.0001"))
        if abs(invoice_line.net_amount - expected_net) > tolerance:
            raise ValidationError(
                f"صافي فاتورة الصنف {receipt_line.product.code} لا يطابق تكلفة الكمية المفوترة من إذن الاستلام."
            )
        prior_lines = list(PurchaseInvoiceLine.objects.filter(
            receipt_line_id=receipt_line.pk,
            invoice__status=PurchaseInvoice.Status.POSTED,
        ).exclude(invoice_id=invoice.pk).select_related("tax_code"))
        for prior_line in prior_lines:
            if prior_line.tax_code_id != invoice_line.tax_code_id or prior_line.tax_rate != invoice_line.tax_rate:
                raise ValidationError(
                    f"لا يمكن تغيير المعاملة الضريبية بين فواتير نفس سطر الاستلام للصنف {receipt_line.product.code}."
                )
            prior_tax_per_unit = prior_line.tax_amount / prior_line.billed_quantity
            current_tax_per_unit = invoice_line.tax_amount / invoice_line.billed_quantity
            if abs(prior_tax_per_unit - current_tax_per_unit) > Decimal("0.0001"):
                raise ValidationError(
                    f"ضريبة الوحدة غير متسقة بين الفواتير الجزئية للصنف {receipt_line.product.code}."
                )
        order_link = PurchaseOrderReceiptLine.objects.select_related("order_line").filter(
            receipt_line_id=receipt_line.pk
        ).first()
        if order_link:
            order_line = order_link.order_line
            if order_link.quantity != receipt_line.quantity:
                raise ValidationError("كمية ربط الاستلام بأمر الشراء غير متطابقة.")
            if order_line.unit_price > 0 and abs(invoice_line.unit_price - order_line.unit_price) > tolerance:
                raise ValidationError(
                    f"سعر فاتورة الصنف {receipt_line.product.code} لا يطابق سعر أمر الشراء بعد الخصم."
                )
            if order_line.tax_rate > 0 and abs(invoice_line.tax_rate - order_line.tax_rate) > tolerance:
                raise ValidationError(
                    f"معدل ضريبة الصنف {receipt_line.product.code} لا يطابق المعدل المتفق عليه في أمر الشراء."
                )
        grni_amount += expected_net
    grni_amount = grni_amount.quantize(Decimal("0.0001"))
    net_amount = sum((line.net_amount for line in lines), Decimal("0")).quantize(Decimal("0.0001"))
    tax_amount = sum((line.tax_amount for line in lines), Decimal("0")).quantize(Decimal("0.0001"))
    recoverable_tax = sum(
        (line.tax_amount for line in lines if not line.tax_code_id or line.tax_code.input_tax_recoverable),
        Decimal("0"),
    ).quantize(Decimal("0.0001"))
    nonrecoverable_tax = (tax_amount - recoverable_tax).quantize(Decimal("0.0001"))
    if net_amount != grni_amount:
        raise ValidationError(
            f"صافي الفاتورة ({net_amount}) لا يساوي قيمة الاستلام ({grni_amount}); راجع أسعار الفاتورة قبل الترحيل."
        )
    if tax_amount < 0:
        raise ValidationError("قيمة الضريبة غير صحيحة.")
    if invoice.transaction_currency_id:
        from accounting.fx_arap import validate_snapshot
        approved = ExchangeRate.objects.filter(
            company=invoice.company, currency=invoice.transaction_currency,
            rate_date=invoice.exchange_rate_date,
        ).first()
        if not approved:
            raise ValidationError("لا يوجد سعر صرف معتمد بتاريخ فاتورة المورد.")
        foreign_net = sum((line.transaction_net_amount or Decimal("0") for line in lines), Decimal("0"))
        foreign_tax = sum((line.transaction_tax_amount or Decimal("0") for line in lines), Decimal("0"))
        foreign_total = foreign_net + foreign_tax
        try:
            expected_base = validate_snapshot(
                base_currency_code=invoice.company.base_currency.code,
                currency_code=invoice.transaction_currency.code,
                foreign_amount=foreign_total, rate=invoice.exchange_rate,
                rate_date=invoice.exchange_rate_date, document_date=invoice.invoice_date,
                approved_rate=approved.rate,
            )
        except ValueError:
            raise ValidationError("تعذر التحقق من سعر الصرف لفاتورة المورد؛ راجع بيانات العملة والسعر المعتمد.") from None
        if abs(expected_base - (net_amount + tax_amount)) > Decimal("0.0002") * max(len(lines), 1):
            raise ValidationError("قيمة فاتورة المورد بالعملة الأساسية لا تطابق إجماليها الأجنبي وسعر الصرف.")
        invoice.transaction_net_total = foreign_net
        invoice.transaction_tax_total = foreign_tax
        invoice.transaction_grand_total = foreign_total
    elif any(line.transaction_unit_price is not None for line in lines):
        raise ValidationError("توجد أسعار أجنبية دون تحديد عملة فاتورة المورد.")

    allocations_by_branch = defaultdict(lambda: {
        "inventory": Decimal("0"), "cogs": Decimal("0"), "loss": Decimal("0"),
    })
    if nonrecoverable_tax:
        # Non-recoverable input tax is held in the clearing account until the
        # complete receipt line has been invoiced. At that point capitalize the
        # cumulative tax once across its remaining stock and consumed cost.
        from types import SimpleNamespace
        for line in lines:
            if not line.tax_code_id or line.tax_code.input_tax_recoverable:
                continue
            receipt_line = line.receipt_line
            prior = PurchaseInvoiceLine.objects.filter(
                receipt_line_id=receipt_line.pk,
                invoice__status=PurchaseInvoice.Status.POSTED,
            ).exclude(invoice_id=invoice.pk).aggregate(
                quantity=Sum("billed_quantity"), tax=Sum("tax_amount"),
            )
            prior_quantity = prior["quantity"] or Decimal("0")
            cumulative_quantity = prior_quantity + line.billed_quantity
            if cumulative_quantity < receipt_line.quantity:
                continue
            if cumulative_quantity > receipt_line.quantity:
                raise ValidationError("الكمية المفوترة تجاوزت كمية الاستلام أثناء توزيع الضريبة.")
            cumulative_tax = (prior["tax"] or Decimal("0")) + line.tax_amount
            if cumulative_tax:
                proxy_line = SimpleNamespace(
                    invoice=invoice, receipt_line=receipt_line, tax_amount=cumulative_tax,
                )
                line_allocations = _capitalize_nonrecoverable_stock_tax(proxy_line)
                for branch_id, amounts in line_allocations.items():
                    for category, amount in amounts.items():
                        allocations_by_branch[branch_id][category] += amount

    payable = _account(invoice.company, AccountRole.Role.ACCOUNTS_PAYABLE, invoice.activity)
    grni = _account(invoice.company, AccountRole.Role.GOODS_RECEIVED_NOT_INVOICED, invoice.activity)
    vat_input = _account(invoice.company, AccountRole.Role.VAT_INPUT, invoice.activity) if recoverable_tax > 0 else None
    clearing = _account(invoice.company, AccountRole.Role.INTER_BRANCH_CLEARING, invoice.activity) if nonrecoverable_tax > 0 else None
    account_list = [payable, grni]
    account_list.extend(account for account in (vat_input, clearing) if account)
    if len({account.pk for account in account_list}) != len(account_list):
        raise ValidationError("يجب فصل حسابات الموردين وGRNI وضريبة المدخلات وتسوية الفروع.")

    allocation_totals = {
        category: sum((amounts[category] for amounts in allocations_by_branch.values()), Decimal("0"))
        for category in ("inventory", "cogs", "loss")
    }
    allocation_accounts = {}
    if allocation_totals["inventory"]:
        allocation_accounts["inventory"] = _account(invoice.company, AccountRole.Role.INVENTORY, invoice.activity)
    if allocation_totals["cogs"]:
        allocation_accounts["cogs"] = _account(invoice.company, AccountRole.Role.COGS, invoice.activity)
    if allocation_totals["loss"]:
        allocation_accounts["loss"] = _account(invoice.company, AccountRole.Role.INVENTORY_ADJUSTMENT_LOSS, invoice.activity)
    if allocation_accounts:
        used = {account.pk for account in [*account_list, *allocation_accounts.values()]}
        if len(used) != len(account_list) + len(allocation_accounts):
            raise ValidationError("يجب فصل حسابات المخزون والتكلفة وخسائر الجرد عن حسابات التسوية.")

    branch, year = _branch_and_year(receipt, invoice.invoice_date)
    entry = JournalEntry.objects.create(
        company=invoice.company, branch=branch, fiscal_year=year,
        entry_number=f"PUR-INV-{invoice.pk}", entry_date=invoice.invoice_date,
        description=f"فاتورة مشتريات المورد {invoice.supplier.name_ar} رقم {invoice.invoice_number}"[:500],
        source_type=JournalEntry.Source.PURCHASES, created_by=user,
    )
    journal_lines = [
        JournalLine(entry=entry, account=grni, description="تسوية بضاعة مستلمة غير مفوترة", debit=grni_amount, credit=0),
        JournalLine(entry=entry, account=payable, description="إثبات مستحقات المورد", debit=0, credit=net_amount + tax_amount),
    ]
    if vat_input and recoverable_tax:
        journal_lines.append(JournalLine(
            entry=entry, account=vat_input, description="ضريبة مدخلات قابلة للاسترداد",
            debit=recoverable_tax, credit=0,
        ))
    if clearing and nonrecoverable_tax:
        journal_lines.append(JournalLine(
            entry=entry, account=clearing, description="تسوية رسملة ضريبة مدخلات غير قابلة للخصم",
            debit=nonrecoverable_tax, credit=0,
        ))
    JournalLine.objects.bulk_create(journal_lines)
    try:
        posted_entry = post_journal(entry.pk, actor=user)
    except DjangoValidationError as exc:
        detail = exc.message_dict if hasattr(exc, "message_dict") else exc.messages
        raise ValidationError(detail) from exc

    for branch_id, amounts in sorted(allocations_by_branch.items()):
        inventory_amount = amounts["inventory"]
        cogs_amount = amounts["cogs"]
        loss_amount = amounts["loss"]
        allocation_total = inventory_amount + cogs_amount + loss_amount
        if allocation_total <= 0:
            continue
        allocation_branch = Branch.objects.get(pk=branch_id, company=invoice.company, is_active=True)
        allocation_year = FiscalYear.objects.filter(
            company=invoice.company, status=FiscalYear.Status.OPEN,
            start_date__lte=invoice.invoice_date, end_date__gte=invoice.invoice_date,
        ).order_by("-start_date").first()
        if not allocation_year:
            raise ValidationError("لا توجد سنة مالية مفتوحة تشمل تاريخ رسملة الضريبة.")
        allocation_entry = JournalEntry.objects.create(
            company=invoice.company, branch=allocation_branch, fiscal_year=allocation_year,
            entry_number=f"PUR-INV-TAX-{invoice.pk}-{branch_id}",
            entry_date=invoice.invoice_date,
            description=f"توزيع ضريبة غير قابلة للخصم لفاتورة {invoice.invoice_number}"[:500],
            source_type=JournalEntry.Source.PURCHASES, created_by=user,
        )
        allocation_lines = []
        if inventory_amount:
            allocation_lines.append(JournalLine(
                entry=allocation_entry, account=allocation_accounts["inventory"],
                description="رسملة الضريبة على المخزون المتبقي", debit=inventory_amount, credit=0,
            ))
        if cogs_amount:
            allocation_lines.append(JournalLine(
                entry=allocation_entry, account=allocation_accounts["cogs"],
                description="تحميل الضريبة على تكلفة البضاعة المستهلكة", debit=cogs_amount, credit=0,
            ))
        if loss_amount:
            allocation_lines.append(JournalLine(
                entry=allocation_entry, account=allocation_accounts["loss"],
                description="تحميل الضريبة على عجز وتسويات المخزون", debit=loss_amount, credit=0,
            ))
        allocation_lines.append(JournalLine(
            entry=allocation_entry, account=clearing,
            description="مقابل توزيع الضريبة غير القابلة للخصم",
            debit=0, credit=allocation_total,
        ))
        JournalLine.objects.bulk_create(allocation_lines)
        try:
            allocation_entry = post_journal(allocation_entry.pk, actor=user)
        except DjangoValidationError as exc:
            detail = exc.message_dict if hasattr(exc, "message_dict") else exc.messages
            raise ValidationError(detail) from exc
        PurchaseInvoiceTaxAllocation.objects.create(
            invoice=invoice, branch=allocation_branch, journal_entry=allocation_entry,
            inventory_amount=inventory_amount, cogs_amount=cogs_amount,
            adjustment_loss_amount=loss_amount,
        )

    invoice.net_total = net_amount
    invoice.tax_total = tax_amount
    invoice.grand_total = net_amount + tax_amount
    invoice.journal_entry = posted_entry
    invoice.status = PurchaseInvoice.Status.POSTED
    invoice.posted_at = timezone.now()
    invoice.save(update_fields=("net_total", "tax_total", "grand_total", "journal_entry", "status", "posted_at"))
    return invoice

@transaction.atomic
def post_supplier_payment(payment_id, user):
    """ترحيل سداد المورد مع تخصيص القيمة الدفترية وفروق العملة المحققة."""
    from accounting.fx_arap import settle_foreign_balance, validate_snapshot
    from .models import SupplierPayment

    payment = SupplierPayment.objects.select_for_update().get(pk=payment_id)
    if payment.status != SupplierPayment.Status.DRAFT:
        raise ValidationError("سند صرف المورد مرحّل بالفعل.")
    from approvals.models import FinancialApprovalRequest
    from approvals.services import require_financial_approval
    try:
        require_financial_approval(FinancialApprovalRequest.DocumentType.SUPPLIER_PAYMENT, payment)
    except DjangoValidationError as exc:
        raise ValidationError(exc.message_dict if hasattr(exc, "message_dict") else exc.messages) from exc
    invoice = PurchaseInvoice.objects.select_for_update().get(pk=payment.invoice_id)
    if invoice.status != PurchaseInvoice.Status.POSTED:
        raise ValidationError("لا يمكن صرف قيمة فاتورة غير مرحلة.")
    if payment.company_id != invoice.company_id or payment.supplier_id != invoice.supplier_id:
        raise ValidationError("بيانات السند لا تطابق فاتورة المورد.")

    if invoice.transaction_currency_id:
        if payment.transaction_currency_id != invoice.transaction_currency_id:
            raise ValidationError("عملة السداد يجب أن تطابق عملة فاتورة المورد.")
        approved = ExchangeRate.objects.filter(
            company=payment.company, currency=payment.transaction_currency,
            rate_date=payment.exchange_rate_date,
        ).first()
        if not approved:
            raise ValidationError("لا يوجد سعر صرف معتمد بتاريخ السداد.")
        try:
            expected_amount = validate_snapshot(
                base_currency_code=payment.company.base_currency.code,
                currency_code=payment.transaction_currency.code,
                foreign_amount=payment.transaction_amount, rate=payment.exchange_rate,
                rate_date=payment.exchange_rate_date, document_date=payment.payment_date,
                approved_rate=approved.rate,
            )
        except (ValueError, TypeError):
            raise ValidationError("تعذر التحقق من سعر الصرف لسند سداد المورد.") from None
        if expected_amount != payment.amount:
            raise ValidationError("مبلغ السداد الأساسي لا يطابق المبلغ الأجنبي مضروبًا في سعر الصرف.")
        from accounting.fx_settlement import allocate_carrying_base_amount, calculate_realized_fx
        from accounting.fx_subledger_services import open_fx_invoice_balances
        from accounting.models import FxSubledgerRevaluation
        if SupplierPayment.objects.filter(
            invoice=invoice, status=SupplierPayment.Status.POSTED,
            payment_date__gt=payment.payment_date,
        ).exists() or FxSubledgerRevaluation.objects.filter(
            purchase_invoice=invoice, revaluation_date__gte=payment.payment_date,
        ).exists() or SupplierReturn.objects.filter(
            invoice=invoice,
            status__in=(SupplierReturn.Status.POSTED, SupplierReturn.Status.REVERSED),
            return_date__gte=payment.payment_date,
        ).exists():
            raise ValidationError("لا يمكن ترحيل سداد بتاريخ يسبق حركة سداد أو مرتجع أو إعادة تقييم مرحّلة.")
        remaining_foreign, historical_remaining, current_remaining = open_fx_invoice_balances(
            invoice, direction="payable", as_of=payment.payment_date,
        )
        try:
            settlement = settle_foreign_balance(
                direction="payable", remaining_foreign_amount=remaining_foreign,
                remaining_carrying_base_amount=current_remaining,
                settled_foreign_amount=payment.transaction_amount,
                settlement_rate=payment.exchange_rate,
            )
            historical_carrying = allocate_carrying_base_amount(
                remaining_foreign, historical_remaining, payment.transaction_amount,
            )
            realized_gain, realized_loss = calculate_realized_fx(
                "payable", historical_carrying, settlement.settlement_base_amount,
            )
        except ValueError:
            raise ValidationError("تعذر احتساب تسوية العملة لسداد المورد.") from None
        carrying_amount = settlement.carrying_base_amount
        unrealized_adjustment = carrying_amount - historical_carrying
    else:
        if any(value is not None for value in (
            payment.transaction_currency_id, payment.transaction_amount,
            payment.exchange_rate, payment.exchange_rate_date,
        )):
            raise ValidationError("لا يمكن استخدام بيانات عملة أجنبية مع فاتورة بالعملة الأساسية.")
        paid_before = SupplierPayment.objects.filter(
            invoice=invoice, status=SupplierPayment.Status.POSTED
        ).exclude(pk=payment.pk).aggregate(total=Sum("amount"))["total"] or Decimal("0")
        returned_before = SupplierReturn.objects.filter(
            invoice=invoice, status=SupplierReturn.Status.POSTED,
        ).aggregate(total=Sum("grand_total"))["total"] or Decimal("0")
        if paid_before + payment.amount + returned_before > invoice.grand_total:
            remaining = max(Decimal("0"), invoice.grand_total - paid_before - returned_before)
            raise ValidationError(f"قيمة السند تتجاوز المتبقي على الفاتورة بعد المرتجعات ({remaining}).")
        carrying_amount = payment.amount
        historical_carrying = payment.amount
        unrealized_adjustment = Decimal("0")
        realized_gain = realized_loss = Decimal("0")

    branch, year = _branch_and_year_for_invoice(invoice, payment.payment_date)
    payable = _account(payment.company, AccountRole.Role.ACCOUNTS_PAYABLE, invoice.activity)
    cash_role = AccountRole.Role.CASH if payment.method == SupplierPayment.Method.CASH else AccountRole.Role.BANK
    payment_account = payment.payment_account or _account(payment.company, cash_role, invoice.activity)
    if payment_account.company_id != payment.company_id or payment_account.nature != "asset" or not payment_account.is_active or payment_account.is_group or not payment_account.allow_posting:
        raise ValidationError("حساب الصرف يجب أن يكون حساب خزينة/بنك نشطًا وقابلًا للترحيل تابعًا للشركة.")
    if payment.payment_account_id:
        from treasury.models import TreasuryAccount
        expected_kind = TreasuryAccount.Kind.CASH if payment.method == SupplierPayment.Method.CASH else TreasuryAccount.Kind.BANK
        treasury_account = TreasuryAccount.objects.filter(company=payment.company, account=payment_account, kind=expected_kind, is_active=True).first()
        if (not treasury_account or (treasury_account.branch_id and treasury_account.branch_id != branch.pk)
                or treasury_account.currency_code != payment.company.base_currency.code):
            raise ValidationError("حساب الخزينة المختار يجب أن يكون بعملة الشركة الأساسية ومطابقًا للفرع والطريقة.")
    if payable.pk == payment_account.pk:
        raise ValidationError("حساب الموردين وحساب الصندوق/البنك يجب أن يكونا مختلفين.")

    # Serialize with treasury postings on the same TreasuryAccount row.
    from treasury.models import TreasuryAccount
    treasury_account = TreasuryAccount.objects.select_for_update().filter(
        company_id=payment.company_id, account_id=payment_account.pk,
    ).first()
    if treasury_account:
        opening_balance = (
            Decimal("0") if treasury_account.opening_journal_entry_id
            else treasury_account.opening_balance
        )
    else:
        ChartOfAccount.objects.select_for_update().get(pk=payment_account.pk)
        opening_balance = Decimal("0")

    balance_movements = JournalLine.objects.filter(
        account_id=payment_account.pk,
        entry__status__in=(JournalEntry.Status.POSTED, JournalEntry.Status.REVERSED),
    ).values("entry_id", "entry__entry_date").annotate(
        debit=Sum("debit"), credit=Sum("credit"),
    ).order_by("entry__entry_date", "entry_id")

    # A backdated payment reduces every later balance too. Include all posted
    # movements after the payment date and reject if any later balance turns
    # negative, not merely the balance on the payment date.
    cash_balance = opening_balance
    later_movements = []
    for movement in balance_movements:
        change = (
            (movement["debit"] or Decimal("0"))
            - (movement["credit"] or Decimal("0"))
        )
        if movement["entry__entry_date"] <= payment.payment_date:
            cash_balance += change
        else:
            later_movements.append(change)

    if cash_balance < payment.amount:
        raise ValidationError(
            f"لا يمكن ترحيل السداد: الرصيد المتاح حتى تاريخ السداد ({cash_balance}) أقل من المبلغ ({payment.amount})."
        )
    for change in later_movements:
        cash_balance += change
        if cash_balance < payment.amount:
            raise ValidationError(
                "لا يمكن ترحيل السداد بأثر رجعي؛ سيؤدي إلى رصيد نقدية/بنك سالب بعد حركة لاحقة مسجلة."
            )

    journal_lines = [
        JournalLine(account=payable, description="تخفيض مستحقات المورد بالقيمة الدفترية", debit=carrying_amount, credit=0),
        JournalLine(account=payment_account, description="صرف نقدي/بنكي للمورد", debit=0, credit=payment.amount),
    ]
    gain = _account(payment.company, AccountRole.Role.REALIZED_FX_GAIN, invoice.activity) if realized_gain else None
    loss = _account(payment.company, AccountRole.Role.REALIZED_FX_LOSS, invoice.activity) if realized_loss else None
    unrealized_gain = _account(payment.company, AccountRole.Role.UNREALIZED_FX_GAIN, invoice.activity) if unrealized_adjustment < 0 else None
    unrealized_loss = _account(payment.company, AccountRole.Role.UNREALIZED_FX_LOSS, invoice.activity) if unrealized_adjustment > 0 else None
    used_ids = {payable.pk, payment_account.pk}
    for account in (gain, loss, unrealized_gain, unrealized_loss):
        if account and account.pk in used_ids:
            raise ValidationError("حسابات فروق العملة يجب أن تكون مستقلة عن الموردين والنقدية.")
        if account:
            used_ids.add(account.pk)
    if realized_gain:
        journal_lines.append(JournalLine(account=gain, description="أرباح فروق عملة محققة على أساس التكلفة التاريخية", debit=0, credit=realized_gain))
    if realized_loss:
        journal_lines.append(JournalLine(account=loss, description="خسائر فروق عملة محققة على أساس التكلفة التاريخية", debit=realized_loss, credit=0))
    if unrealized_adjustment > 0:
        journal_lines.append(JournalLine(account=unrealized_loss, description="عكس خسارة إعادة التقييم غير المحققة عند السداد", debit=0, credit=unrealized_adjustment))
    elif unrealized_adjustment < 0:
        journal_lines.append(JournalLine(account=unrealized_gain, description="عكس ربح إعادة التقييم غير المحقق عند السداد", debit=-unrealized_adjustment, credit=0))

    entry = JournalEntry.objects.create(
        company=payment.company, branch=branch, fiscal_year=year,
        entry_number=f"PUR-PAY-{payment.pk}", entry_date=payment.payment_date,
        description=f"سداد فاتورة المورد {invoice.invoice_number} - سند {payment.payment_number}"[:500],
        source_type=JournalEntry.Source.PURCHASES, created_by=user,
    )
    for line in journal_lines:
        line.entry = entry
    JournalLine.objects.bulk_create(journal_lines)
    try:
        posted_entry = post_journal(entry.pk, actor=user)
    except DjangoValidationError as exc:
        detail = exc.message_dict if hasattr(exc, "message_dict") else exc.messages
        raise ValidationError(detail) from exc

    if payment.method == SupplierPayment.Method.BANK:
        from treasury.models import TreasuryTransaction
        from treasury.services import record_posted_document_bank_movement
        record_posted_document_bank_movement(
            company=payment.company, branch=branch, fiscal_year=year, account=payment_account,
            kind=TreasuryTransaction.Kind.PAYMENT, amount=payment.amount,
            transaction_date=payment.payment_date, document_number=f"AUTO-PUR-PAY-{payment.pk}",
            description=f"سداد فاتورة مشتريات {invoice.invoice_number} - سند {payment.payment_number}",
            counter_account=payable, journal_entry=posted_entry, user=user, reference=payment.reference,
        )
    payment.carrying_base_amount = carrying_amount
    payment.historical_carrying_base_amount = historical_carrying
    payment.realized_fx_gain = realized_gain
    payment.realized_fx_loss = realized_loss
    payment.journal_entry = posted_entry
    payment.status = SupplierPayment.Status.POSTED
    payment.posted_at = timezone.now()
    payment.save(update_fields=("carrying_base_amount", "historical_carrying_base_amount", "realized_fx_gain", "realized_fx_loss", "journal_entry", "status", "posted_at"))
    return payment


@transaction.atomic


def supplier_credit_balance(company_id, supplier_id, as_of):
    """رصيد المورد الدائن المتاح للاسترداد بعد احتساب الفواتير والسداد والمرتجعات السابقة."""
    invoice_total = PurchaseInvoice.objects.filter(
        company_id=company_id, supplier_id=supplier_id,
        status=PurchaseInvoice.Status.POSTED, invoice_date__lte=as_of,
    ).aggregate(total=Sum("grand_total"))["total"] or Decimal("0")
    payments = SupplierPayment.objects.filter(
        company_id=company_id, supplier_id=supplier_id,
        status=SupplierPayment.Status.POSTED, payment_date__lte=as_of,
    )
    payment_total = sum((
        payment.carrying_base_amount if payment.carrying_base_amount is not None else payment.amount
        for payment in payments
    ), Decimal("0"))
    active_returns = SupplierReturn.objects.filter(
        company_id=company_id, supplier_id=supplier_id, return_date__lte=as_of,
    ).filter(
        Q(status=SupplierReturn.Status.POSTED)
        | Q(status=SupplierReturn.Status.REVERSED, reversal_date__gt=as_of)
    )
    return_total = active_returns.aggregate(total=Sum("grand_total"))["total"] or Decimal("0")
    refund_total = SupplierRefund.objects.filter(
        company_id=company_id, supplier_id=supplier_id,
        status=SupplierRefund.Status.POSTED, refund_date__lte=as_of,
    ).aggregate(total=Sum("amount"))["total"] or Decimal("0")
    revaluation_total = FxSubledgerRevaluation.objects.filter(
        company_id=company_id,
        purchase_invoice__supplier_id=supplier_id,
        revaluation_date__lte=as_of,
        journal_entry__status=JournalEntry.Status.POSTED,
    ).aggregate(total=Sum("difference"))["total"] or Decimal("0")
    # Direct fixed-asset invoices and their settlements share the supplier AP subledger.
    from assets.models import AssetPurchaseInvoice, AssetSupplierPayment
    asset_invoice_total = AssetPurchaseInvoice.objects.filter(
        company_id=company_id, supplier_id=supplier_id,
        status=AssetPurchaseInvoice.Status.POSTED, invoice_date__lte=as_of,
    ).aggregate(total=Sum("grand_total"))["total"] or Decimal("0")
    asset_payment_total = AssetSupplierPayment.objects.filter(
        company_id=company_id, supplier_id=supplier_id,
        status=AssetSupplierPayment.Status.POSTED, payment_date__lte=as_of,
    ).aggregate(total=Sum("amount"))["total"] or Decimal("0")
    supplier_balance = (
        invoice_total + asset_invoice_total - payment_total - asset_payment_total
        - return_total + refund_total + revaluation_total
    )
    return max(Decimal("0"), -supplier_balance).quantize(Decimal("0.0001"))


@transaction.atomic
def post_supplier_refund(refund_id, user):
    """ترحيل استرداد نقدي من المورد ضمن الرصيد الدائن المتاح."""
    document = SupplierRefund.objects.select_for_update(of=("self",)).select_related(
        "company", "supplier", "return_document", "return_document__invoice__receipt",
        "return_document__invoice__receipt__warehouse", "payment_account",
    ).get(pk=refund_id)
    if document.status != SupplierRefund.Status.DRAFT:
        raise ValidationError("استرداد المورد مرحّل بالفعل.")
    return_document = SupplierReturn.objects.select_for_update().get(pk=document.return_document_id)
    if return_document.status != SupplierReturn.Status.POSTED:
        raise ValidationError("لا يمكن استرداد مبلغ عن مرتجع غير مرحّل أو معكوس.")
    if document.refund_date < return_document.return_date:
        raise ValidationError("تاريخ الاسترداد لا يجوز أن يسبق تاريخ المرتجع.")
    available_credit = supplier_credit_balance(
        document.company_id, document.supplier_id, document.refund_date,
    )
    if document.amount > available_credit:
        raise ValidationError(
            f"قيمة الاسترداد تتجاوز الرصيد الدائن المتاح للمورد ({available_credit})."
        )
    payable = _account(document.company, AccountRole.Role.ACCOUNTS_PAYABLE, return_document.invoice.activity)
    if document.payment_account_id == payable.pk:
        raise ValidationError("حساب الاسترداد يجب أن يختلف عن حساب الموردين.")
    from treasury.models import TreasuryAccount
    expected_kind = (
        TreasuryAccount.Kind.CASH
        if document.method == SupplierRefund.Method.CASH
        else TreasuryAccount.Kind.BANK
    )
    treasury_account = TreasuryAccount.objects.filter(
        company=document.company, account=document.payment_account,
        kind=expected_kind, is_active=True,
    ).first()
    branch = resolve_warehouse_branch(document.company, return_document.invoice.receipt.warehouse)
    if (
        document.payment_account.company_id != document.company_id
        or document.payment_account.nature != ChartOfAccount.Nature.ASSET
        or not document.payment_account.is_active
        or document.payment_account.is_group
        or not document.payment_account.allow_posting
        or not treasury_account
        or (treasury_account.branch_id and treasury_account.branch_id != (branch.pk if branch else None))
        or treasury_account.currency_code != document.company.base_currency.code
    ):
        raise ValidationError("حساب الاسترداد لم يعد حساب خزينة/بنك نشطًا ومطابقًا لعملة الشركة وطريقة الاسترداد والفرع.")
    branch, year = _branch_and_year(return_document.invoice.receipt, document.refund_date)
    entry = JournalEntry.objects.create(
        company=document.company, branch=branch, fiscal_year=year,
        entry_number=f"PUR-REF-{document.pk}", entry_date=document.refund_date,
        description=f"استرداد نقدي من المورد {document.supplier.name_ar} عن المرتجع {return_document.return_number}"[:500],
        source_type=JournalEntry.Source.PURCHASES, created_by=user,
    )
    JournalLine.objects.bulk_create([
        JournalLine(
            entry=entry, account=document.payment_account,
            description="المبلغ المسترد من المورد", debit=document.amount, credit=0,
        ),
        JournalLine(
            entry=entry, account=payable,
            description="تخفيض الرصيد الدائن للمورد", debit=0, credit=document.amount,
        ),
    ])
    try:
        posted_entry = post_journal(entry.pk, actor=user)
    except DjangoValidationError as exc:
        detail = exc.message_dict if hasattr(exc, "message_dict") else exc.messages
        raise ValidationError(detail) from exc
    if document.method == SupplierRefund.Method.BANK:
        from treasury.models import TreasuryTransaction
        from treasury.services import record_posted_document_bank_movement
        record_posted_document_bank_movement(
            company=document.company, branch=branch, fiscal_year=year,
            account=document.payment_account, kind=TreasuryTransaction.Kind.RECEIPT,
            amount=document.amount, transaction_date=document.refund_date,
            document_number=f"AUTO-PUR-REF-{document.pk}",
            description=f"استرداد من المورد {document.supplier.name_ar} - سند {document.refund_number}",
            counter_account=payable, journal_entry=posted_entry, user=user,
            reference=document.reference,
        )
    document.journal_entry = posted_entry
    document.status = SupplierRefund.Status.POSTED
    document.posted_at = timezone.now()
    document.save(update_fields=("journal_entry", "status", "posted_at"))
    return document


@transaction.atomic
def reverse_supplier_refund(refund_id, user, entry_date):
    """عكس استرداد المورد بقيد عكسي وإعادة الرصيد الدائن للمورد."""
    document = SupplierRefund.objects.select_for_update(of=("self",)).select_related(
        "company", "supplier", "return_document__invoice__receipt__warehouse",
        "journal_entry",
    ).get(pk=refund_id)
    if document.status != SupplierRefund.Status.POSTED or not document.journal_entry_id:
        raise ValidationError("لا يمكن عكس إلا استرداد مورد مرحّل.")
    if document.reversal_journal_entry_id:
        raise ValidationError("تم عكس استرداد المورد من قبل.")
    if entry_date < document.refund_date:
        raise ValidationError("تاريخ العكس لا يجوز أن يسبق تاريخ الاسترداد.")
    if document.journal_entry.status != JournalEntry.Status.POSTED:
        raise ValidationError("القيد الأصلي لاسترداد المورد ليس مرحّلًا.")

    # لا نعكس استردادًا إذا وُجد استرداد مرحّل أحدث منه؛ ويُحسم الترتيب
    # داخل اليوم نفسه برقم السجل حتى لا يحجب كل استردادٍ الآخر.
    later_refunds = SupplierRefund.objects.filter(
        company_id=document.company_id,
        supplier_id=document.supplier_id,
        status=SupplierRefund.Status.POSTED,
    ).filter(
        Q(refund_date__gt=document.refund_date)
        | Q(refund_date=document.refund_date, pk__gt=document.pk)
    )
    if later_refunds.exists():
        raise ValidationError(
            "لا يمكن عكس هذا الاسترداد لوجود استردادات أحدث للمورد؛ "
            "اعكس المستندات الأحدث أولًا."
        )

    # عكس القيد يعيد الرصيد الدائن للمورد، لكنه يخرج النقدية من حساب الدفع.
    # افحص الرصيد الدفتري في تاريخ العكس حتى لا ينتج رصيد خزينة/بنك سالب.
    cash_totals = JournalLine.objects.filter(
        account_id=document.payment_account_id,
        entry__entry_date__lte=entry_date,
        entry__status__in=(JournalEntry.Status.POSTED, JournalEntry.Status.REVERSED),
    ).aggregate(
        debit=Sum("debit"), credit=Sum("credit"),
    )
    from treasury.models import TreasuryAccount
    opening_balance = TreasuryAccount.objects.filter(
        company_id=document.company_id, account_id=document.payment_account_id,
        opening_journal_entry__isnull=True,
    ).aggregate(total=Sum("opening_balance"))["total"] or Decimal("0")
    cash_balance = opening_balance + (cash_totals["debit"] or Decimal("0")) - (cash_totals["credit"] or Decimal("0"))
    if cash_balance < document.amount:
        raise ValidationError(
            f"لا يمكن عكس الاسترداد: رصيد حساب الخزينة/البنك في تاريخ العكس ({cash_balance}) أقل من المبلغ ({document.amount})."
        )

    try:
        reversal_entry = reverse_operational_journal(
            document.journal_entry_id, user, entry_date, JournalEntry.Source.PURCHASES,
        )
        from treasury.services import reverse_linked_document_treasury_movement
        reverse_linked_document_treasury_movement(
            journal_entry_id=document.journal_entry_id,
            reversal_journal_entry=reversal_entry,
            user=user,
        )
    except DjangoValidationError as exc:
        detail = exc.message_dict if hasattr(exc, "message_dict") else exc.messages
        raise ValidationError(detail) from exc

    document.status = SupplierRefund.Status.REVERSED
    document.reversal_journal_entry = reversal_entry
    document.reversal_date = entry_date
    document.reversed_at = timezone.now()
    document.reversed_by = user
    document.save(update_fields=(
        "status", "reversal_journal_entry", "reversal_date", "reversed_at", "reversed_by",
    ))
    AuditEvent.objects.create(
        actor=user, company=document.company,
        branch=document.return_document.invoice.receipt.warehouse.branch,
        action=AuditEvent.Action.ACTION, resource="purchasing.supplier_refund.reversed",
        object_id=str(document.pk), http_method="POST", status_code=200, succeeded=True,
        metadata={
            "refund_number": document.refund_number,
            "reversal_date": entry_date.isoformat(),
            "reversal_journal_entry_id": reversal_entry.pk,
        },
    )
    return document


@transaction.atomic
def post_supplier_return(return_id, user):
    """ترحيل مرتجع المورد من مصدر الاستلام الأصلي مع تسوية المورد والضريبة والمخزون."""
    document = SupplierReturn.objects.select_for_update(of=("self",)).select_related(
        "company", "supplier", "invoice", "invoice__receipt", "invoice__receipt__warehouse",
    ).get(pk=return_id)
    if document.status != SupplierReturn.Status.DRAFT:
        raise ValidationError("مرتجع المورد مرحّل بالفعل.")
    invoice = PurchaseInvoice.objects.select_for_update(of=("self",)).select_related(
        "company", "supplier", "receipt", "receipt__warehouse",
    ).get(pk=document.invoice_id)
    if invoice.transaction_currency_id and FxSubledgerRevaluation.objects.filter(
        purchase_invoice=invoice,
    ).filter(
        Q(journal_entry__isnull=True)
        | ~Q(journal_entry__status=JournalEntry.Status.REVERSED)
    ).exists():
        raise ValidationError("لا يمكن ترحيل مرتجع فاتورة أجنبية بعد إعادة تقييم سارية؛ اعكس التقييم أولًا.")
    if invoice.status != PurchaseInvoice.Status.POSTED:
        raise ValidationError("لا يمكن ترحيل مرتجع قبل ترحيل فاتورة المورد.")
    if document.return_date < invoice.invoice_date:
        raise ValidationError("تاريخ المرتجع لا يجوز أن يسبق تاريخ فاتورة المورد.")
    if document.company_id != invoice.company_id or document.supplier_id != invoice.supplier_id:
        raise ValidationError("بيانات المرتجع لا تطابق فاتورة المورد.")
    lines = list(document.lines.select_for_update(of=("self",)).select_related(
        "invoice_line", "invoice_line__receipt_line", "invoice_line__receipt_line__product",
        "invoice_line__tax_code", "movement_line",
    ))
    if not lines:
        raise ValidationError("أضف بندًا واحدًا على الأقل للمرتجع.")

    posted_returns = SupplierReturnLine.objects.filter(
        invoice_line_id__in=[line.invoice_line_id for line in lines],
        return_document__status=SupplierReturn.Status.POSTED,
    ).values("invoice_line_id").annotate(quantity=Sum("quantity"))
    returned_by_invoice_line = {row["invoice_line_id"]: row["quantity"] for row in posted_returns}
    net_total = Decimal("0")
    tax_total = Decimal("0")
    recoverable_total = Decimal("0")
    inventory_credit = Decimal("0")
    transaction_net_total = Decimal("0")
    transaction_tax_total = Decimal("0")
    movement = document.stock_movement
    if movement.status != StockMovement.Status.DRAFT or movement.movement_type != StockMovement.MovementType.SUPPLIER_RETURN:
        raise ValidationError("حركة المخزون المرتبطة بالمرتجع غير صالحة أو سبق ترحيلها.")
    if movement.source_warehouse_id != invoice.receipt.warehouse_id:
        raise ValidationError("مخزن المرتجع يجب أن يطابق مخزن إذن الاستلام الأصلي في هذه المرحلة.")

    for line in lines:
        invoice_line = line.invoice_line
        receipt_line = invoice_line.receipt_line
        already_returned = returned_by_invoice_line.get(invoice_line.pk, Decimal("0"))
        if already_returned + line.quantity > invoice_line.billed_quantity:
            raise ValidationError(f"كمية المرتجع تتجاوز الكمية المفوترة في هذه الفاتورة للصنف {receipt_line.product.code}.")
        if (
            invoice_line.tax_amount > 0 and invoice_line.tax_code_id
            and not invoice_line.tax_code.input_tax_recoverable
        ):
            billed_total = PurchaseInvoiceLine.objects.filter(
                receipt_line_id=receipt_line.pk,
                invoice__status=PurchaseInvoice.Status.POSTED,
            ).aggregate(total=Sum("billed_quantity"))["total"] or Decimal("0")
            if billed_total < receipt_line.quantity:
                raise ValidationError(
                    f"لا يمكن ترحيل مرتجع الصنف {receipt_line.product.code} قبل اكتمال فوترة إذن الاستلام؛ "
                    "لم يتم بعد توزيع الضريبة غير القابلة للخصم على تكلفة المخزون."
                )
        if line.movement_line.product_id != receipt_line.product_id or line.movement_line.quantity != line.quantity:
            raise ValidationError("كمية أو صنف حركة المرتجع لا يطابق بند فاتورة المورد.")
        if line.movement_line.source_location_id != receipt_line.destination_location_id:
            raise ValidationError("موقع الصرف يجب أن يطابق موقع استلام الصنف الأصلي.")
        if line.movement_line.expiry_date != receipt_line.expiry_date:
            raise ValidationError("تاريخ صلاحية المرتجع يجب أن يطابق دفعة الاستلام الأصلية.")

        ratio = line.quantity / invoice_line.billed_quantity
        line_net = (invoice_line.net_amount * ratio).quantize(Decimal("0.0001"))
        line_tax = (invoice_line.tax_amount * ratio).quantize(Decimal("0.0001"))
        recoverable = line_tax if (
            not invoice_line.tax_code_id or invoice_line.tax_code.input_tax_recoverable
        ) else Decimal("0")
        nonrecoverable = line_tax - recoverable
        line.net_amount = line_net
        line.tax_amount = line_tax
        line.recoverable_tax_amount = recoverable
        line.nonrecoverable_tax_amount = nonrecoverable
        line.save(update_fields=(
            "net_amount", "tax_amount", "recoverable_tax_amount", "nonrecoverable_tax_amount",
        ))
        net_total += line_net
        tax_total += line_tax
        recoverable_total += recoverable
        if invoice.transaction_currency_id:
            if invoice_line.transaction_net_amount is None or invoice_line.transaction_tax_amount is None:
                raise ValidationError("بيانات العملة الأجنبية غير مكتملة في بند فاتورة المورد.")
            transaction_net_total += (
                invoice_line.transaction_net_amount * ratio
            ).quantize(Decimal("0.0001"))
            transaction_tax_total += (
                invoice_line.transaction_tax_amount * ratio
            ).quantize(Decimal("0.0001"))

    # The stock service consumes only layers whose root is the exact receipt line.
    movement = post_stock_movement(movement.pk, user)
    for line in lines:
        line.movement_line.refresh_from_db()
        expected_inventory_credit = line.net_amount + line.nonrecoverable_tax_amount
        actual_cost = line.movement_line.cost_amount
        if abs(actual_cost - expected_inventory_credit) > Decimal("0.0001"):
            raise ValidationError(
                f"تكلفة الصنف المرتجع ({actual_cost}) لا تطابق قيمة المخزون الدفترية "
                f"({expected_inventory_credit}); تم إلغاء الترحيل دون إنشاء قيد."
            )
        inventory_credit += actual_cost

    grand_total = net_total + tax_total
    expected_inventory_credit = net_total + (tax_total - recoverable_total)
    if abs(inventory_credit - expected_inventory_credit) > Decimal("0.0001"):
        raise ValidationError("قيمة تكلفة المخزون المرتجع لا تتطابق مع صافي المرتجع والضريبة غير القابلة للخصم ضمن هامش التقريب.")

    payable = _account(invoice.company, AccountRole.Role.ACCOUNTS_PAYABLE, invoice.activity)
    inventory = _account(invoice.company, AccountRole.Role.INVENTORY, invoice.activity)
    vat_input = _account(invoice.company, AccountRole.Role.VAT_INPUT, invoice.activity) if recoverable_total else None
    if len({account.pk for account in (payable, inventory, *([vat_input] if vat_input else []))}) != (3 if vat_input else 2):
        raise ValidationError("يجب فصل حساب الموردين والمخزون وضريبة المدخلات.")

    branch, year = _branch_and_year(invoice.receipt, document.return_date)
    entry = JournalEntry.objects.create(
        company=invoice.company, branch=branch, fiscal_year=year,
        entry_number=f"PUR-RET-{document.pk}", entry_date=document.return_date,
        description=f"مرتجع مورد {invoice.supplier.name_ar} عن الفاتورة {invoice.invoice_number}"[:500],
        source_type=JournalEntry.Source.PURCHASES, created_by=user,
    )
    journal_lines = [
        JournalLine(entry=entry, account=payable, description="تخفيض مستحقات المورد بالمرتجع", debit=grand_total, credit=0),
        JournalLine(entry=entry, account=inventory, description="إخراج تكلفة البضاعة المرتجعة", debit=0, credit=inventory_credit),
    ]
    if recoverable_total:
        journal_lines.append(JournalLine(
            entry=entry, account=vat_input, description="عكس ضريبة المدخلات القابلة للاسترداد",
            debit=0, credit=recoverable_total,
        ))
    JournalLine.objects.bulk_create(journal_lines)
    try:
        posted_entry = post_journal(entry.pk, actor=user)
    except DjangoValidationError as exc:
        detail = exc.message_dict if hasattr(exc, "message_dict") else exc.messages
        raise ValidationError(detail) from exc

    document.net_amount = net_total
    document.tax_amount = tax_total
    document.grand_total = grand_total
    if invoice.transaction_currency_id:
        document.transaction_net_amount = transaction_net_total
        document.transaction_tax_amount = transaction_tax_total
        document.transaction_grand_total = transaction_net_total + transaction_tax_total
    document.journal_entry = posted_entry
    document.status = SupplierReturn.Status.POSTED
    document.posted_at = timezone.now()
    document.save(update_fields=(
        "net_amount", "tax_amount", "grand_total", "transaction_net_amount", "transaction_tax_amount",
        "transaction_grand_total", "journal_entry", "status", "posted_at",
    ))
    return document


@transaction.atomic
def reverse_supplier_return(return_id, user, entry_date):
    """عكس مرتجع المورد بقيد عكسي وإرجاع نفس طبقات التكلفة التي استهلكها المرتجع."""
    document = SupplierReturn.objects.select_for_update(of=("self",)).select_related(
        "company", "supplier", "invoice", "journal_entry", "stock_movement",
    ).get(pk=return_id)
    if document.status != SupplierReturn.Status.POSTED or not document.journal_entry_id:
        raise ValidationError("لا يمكن عكس إلا مرتجع مورد مرحّل.")
    if document.reversal_journal_entry_id or document.reversal_stock_movement_id:
        raise ValidationError("تم عكس مرتجع المورد من قبل.")
    if SupplierRefund.objects.filter(
        return_document=document, status=SupplierRefund.Status.POSTED,
    ).exists():
        raise ValidationError("يجب عكس استردادات المورد المرتبطة قبل عكس المرتجع.")
    if entry_date < document.return_date:
        raise ValidationError("تاريخ العكس لا يجوز أن يسبق تاريخ المرتجع.")
    invoice = PurchaseInvoice.objects.select_for_update().get(pk=document.invoice_id)
    if invoice.transaction_currency_id and FxSubledgerRevaluation.objects.filter(
        purchase_invoice=invoice,
    ).filter(
        Q(journal_entry__isnull=True)
        | ~Q(journal_entry__status=JournalEntry.Status.REVERSED)
    ).exists():
        raise ValidationError("لا يمكن عكس مرتجع فاتورة أجنبية بعد إعادة تقييم سارية؛ اعكس التقييم أولًا.")
    if document.journal_entry.status != JournalEntry.Status.POSTED:
        raise ValidationError("القيد الأصلي للمرتجع ليس مرحّلًا.")
    lines = list(document.lines.select_for_update(of=("self",)).select_related(
        "invoice_line__receipt_line__product", "movement_line",
    ).order_by("id"))
    if not lines:
        raise ValidationError("لا توجد بنود مخزون مرتبطة بالمرتجع.")

    reversal_movement = StockMovement.objects.create(
        company=document.company,
        movement_number=f"SRV-{document.pk}",
        movement_type=StockMovement.MovementType.SUPPLIER_RETURN_REVERSAL,
        movement_date=entry_date,
        destination_warehouse=invoice.receipt.warehouse,
        notes=f"عكس مرتجع المورد {document.return_number}",
        created_by=user,
    )
    for line in lines:
        product = line.invoice_line.receipt_line.product
        if product.costing_method not in (
            Product.CostingMethod.FIFO, Product.CostingMethod.WEIGHTED_AVERAGE,
        ):
            raise ValidationError("لا يمكن عكس المرتجع بعد تغيير طريقة تكلفة الصنف من FIFO/المتوسط المرجح.")
        reversal_line = StockMovementLine.objects.create(
            movement=reversal_movement,
            product=product,
            quantity=line.quantity,
            destination_location=line.movement_line.source_location,
            expiry_date=line.movement_line.expiry_date,
        )
        SupplierReturnLine.objects.filter(pk=line.pk).update(reversal_movement_line=reversal_line)
        line.reversal_movement_line = reversal_line

    posted_movement = post_stock_movement(reversal_movement.pk, user)
    for line in lines:
        line.reversal_movement_line.refresh_from_db()
        if abs(line.reversal_movement_line.cost_amount - line.movement_line.cost_amount) > Decimal("0.0001"):
            raise ValidationError("تكلفة المخزون المسترد لا تطابق تكلفة المرتجع الأصلي؛ تم إلغاء العكس.")

    try:
        reversal_entry = reverse_operational_journal(
            document.journal_entry_id, user, entry_date, JournalEntry.Source.PURCHASES,
        )
    except DjangoValidationError as exc:
        detail = exc.message_dict if hasattr(exc, "message_dict") else exc.messages
        raise ValidationError(detail) from exc

    document.reversal_stock_movement = posted_movement
    document.reversal_journal_entry = reversal_entry
    document.reversal_date = entry_date
    document.reversed_at = timezone.now()
    document.status = SupplierReturn.Status.REVERSED
    document.save(update_fields=(
        "reversal_stock_movement", "reversal_journal_entry", "reversal_date",
        "reversed_at", "status",
    ))
    return document



def _landed_cost_branch(company, warehouse):
    branch = resolve_warehouse_branch(company, warehouse)
    if not branch or not branch.is_active:
        raise ValidationError("لا يمكن رسملة تكلفة على مخزن بلا فرع نشط.")
    return branch


def _allocate_landed_cost_to_stock(allocation):
    """Split one landed-cost amount over remaining stock, COGS, and stock losses."""
    receipt_line = allocation.receipt_line
    amount = allocation.amount
    quantity = receipt_line.quantity
    company = allocation.document.company
    if quantity <= 0 or amount <= 0:
        raise ValidationError("كمية الاستلام ومبلغ الرسملة يجب أن يكونا موجبين.")
    if receipt_line.movement.status != StockMovement.Status.POSTED:
        raise ValidationError("لا يمكن رسملة بند استلام غير مرحّل.")
    if receipt_line.product.costing_method not in (
        Product.CostingMethod.FIFO, Product.CostingMethod.WEIGHTED_AVERAGE,
    ):
        raise ValidationError("طريقة تكلفة الصنف لا تدعم رسملة التكلفة الإضافية.")

    layers = list(StockCostLayer.objects.select_for_update(of=("self",)).filter(
        root_source_line=receipt_line,
    ).select_related("warehouse", "warehouse__branch").order_by("pk"))
    if not layers:
        raise ValidationError(
            f"لا توجد طبقات تكلفة مرتبطة بالاستلام الأصلي للصنف {receipt_line.product.code}."
        )

    remaining_by_balance = defaultdict(Decimal)
    remaining_quantity = Decimal("0")
    for layer in layers:
        if layer.quantity_remaining <= 0:
            continue
        key = (layer.warehouse_id, layer.location_id, layer.expiry_date)
        remaining_by_balance[key] += layer.quantity_remaining
        remaining_quantity += layer.quantity_remaining

    consumed_by_branch = defaultdict(lambda: {"cogs": Decimal("0"), "loss": Decimal("0")})
    consumed_quantity = Decimal("0")
    consumptions = StockCostConsumption.objects.select_for_update(of=("self",)).filter(
        source_layer__root_source_line=receipt_line,
        movement_line__movement__status=StockMovement.Status.POSTED,
    ).select_related(
        "movement_line__movement__source_warehouse",
        "movement_line__movement__destination_warehouse",
    ).order_by("pk")
    for consumption in consumptions:
        movement_line = consumption.movement_line
        movement = movement_line.movement
        if movement.movement_type in (
            StockMovement.MovementType.ISSUE,
            StockMovement.MovementType.SALES_RETURN_REVERSAL,
        ):
            category = "cogs"
        elif (
            movement.movement_type == StockMovement.MovementType.ADJUSTMENT
            and movement_line.adjustment_direction == "out"
        ):
            category = "loss"
        else:
            # Transfers create descendant layers; they are not final consumption.
            continue
        if not movement.source_warehouse_id:
            raise ValidationError("تعذر تحديد مخزن مصدر لاستهلاك طبقة تكلفة.")
        branch = _landed_cost_branch(company, movement.source_warehouse)
        consumed_by_branch[branch.pk][category] += consumption.quantity
        consumed_quantity += consumption.quantity

    traced_quantity = remaining_quantity + consumed_quantity
    if abs(traced_quantity - quantity) > Decimal("0.0001"):
        raise ValidationError(
            f"تتبع تكلفة الصنف {receipt_line.product.code} غير مكتمل: "
            f"الاستلام {quantity}، والمتبقي والمستهلك النهائي {traced_quantity}."
        )

    base_currency = company.base_currency
    quantum = Decimal("1").scaleb(-base_currency.decimal_places)
    unit_addition = (amount / quantity).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)
    buckets = []
    for (warehouse_id, location_id, expiry_date), remaining in remaining_by_balance.items():
        if remaining <= 0:
            continue
        balance = StockBalance.objects.select_for_update().get(
            product_id=receipt_line.product_id,
            warehouse_id=warehouse_id,
            location_id=location_id,
            expiry_date=expiry_date,
        )
        branch = _landed_cost_branch(company, balance.warehouse)
        buckets.append({
            "branch": branch, "kind": "inventory", "balance": balance,
            "raw": remaining * amount / quantity,
        })
    for branch_id, categories in consumed_by_branch.items():
        branch = Branch.objects.get(pk=branch_id, company=company, is_active=True)
        for category in ("cogs", "loss"):
            if categories[category] > 0:
                buckets.append({
                    "branch": branch, "kind": category, "balance": None,
                    "raw": categories[category] * amount / quantity,
                })

    if not buckets:
        raise ValidationError("لا توجد كميات مخزنية أو مستهلكة يمكن تحميل تكلفة عليها.")
    rounded = [
        bucket["raw"].quantize(quantum, rounding=ROUND_HALF_UP)
        for bucket in buckets
    ]
    residual = amount - sum(rounded, Decimal("0"))
    if residual:
        largest = max(range(len(buckets)), key=lambda index: buckets[index]["raw"])
        rounded[largest] += residual
    if any(value < 0 for value in rounded) or sum(rounded, Decimal("0")) != amount:
        raise ValidationError("تعذر موازنة توزيع تكلفة الشراء بعد التقريب.")

    result = defaultdict(lambda: {
        "inventory": Decimal("0"), "cogs": Decimal("0"), "loss": Decimal("0"),
    })
    for bucket, value in zip(buckets, rounded):
        result[bucket["branch"].pk][bucket["kind"]] += value
        if bucket["kind"] == "inventory" and value:
            balance = bucket["balance"]
            balance.inventory_value += value
            if balance.quantity > 0:
                balance.average_cost = (balance.inventory_value / balance.quantity).quantize(
                    Decimal("0.000001"), rounding=ROUND_HALF_UP,
                )
            balance.save(update_fields=("inventory_value", "average_cost", "updated_at"))

    # Update all remaining descendant layers, including layers created by transfers.
    for layer in layers:
        if layer.quantity_remaining > 0:
            layer.unit_cost = (layer.unit_cost + unit_addition).quantize(
                Decimal("0.00000001"), rounding=ROUND_HALF_UP,
            )
            layer.save(update_fields=("unit_cost",))

    return {
        branch_id: amounts for branch_id, amounts in result.items()
        if sum(amounts.values(), Decimal("0")) > 0
    }


def _post_landed_cost_journal(company, branch, entry_date, entry_number, description, lines, user):
    year = FiscalYear.objects.filter(
        company=company, status=FiscalYear.Status.OPEN,
        start_date__lte=entry_date, end_date__gte=entry_date,
    ).order_by("-start_date").first()
    if not year:
        raise ValidationError("لا توجد سنة مالية مفتوحة تشمل تاريخ رسملة التكلفة.")
    entry = JournalEntry.objects.create(
        company=company, branch=branch, fiscal_year=year,
        entry_number=entry_number, entry_date=entry_date,
        description=description[:500], source_type=JournalEntry.Source.PURCHASES,
        created_by=user,
    )
    journal_lines = []
    for row in lines:
        account, line_description, debit, credit = row[:4]
        cost_center = row[4] if len(row) > 4 else None
        if debit or credit:
            journal_lines.append(JournalLine(
                entry=entry, account=account, description=line_description[:240],
                debit=debit, credit=credit, cost_center=cost_center,
            ))
    JournalLine.objects.bulk_create(journal_lines)
    try:
        return post_journal(entry.pk, actor=user)
    except DjangoValidationError as exc:
        detail = exc.message_dict if hasattr(exc, "message_dict") else exc.messages
        raise ValidationError(detail) from exc


@transaction.atomic
def post_landed_cost_document(document_id, user):
    """Capitalize freight/customs/etc. against the already-posted direct expense invoice."""
    document = LandedCostDocument.objects.select_for_update(of=("self",)).select_related(
        "company", "company__base_currency", "source_invoice", "source_invoice__branch",
        "source_invoice__activity",
    ).get(pk=document_id)
    if document.status != LandedCostDocument.Status.DRAFT:
        raise ValidationError("مستند رسملة التكلفة مرحّل بالفعل أو غير قابل للترحيل.")
    invoice = PurchaseInvoice.objects.select_for_update(of=("self",)).select_related(
        "company", "branch", "activity", "supplier",
    ).get(pk=document.source_invoice_id)
    if (
        invoice.status != PurchaseInvoice.Status.POSTED
        or invoice.invoice_type != PurchaseInvoice.InvoiceType.DIRECT_EXPENSE
        or invoice.company_id != document.company_id
        or not invoice.branch_id
    ):
        raise ValidationError("فاتورة مصروف المصدر غير صالحة لرسملة التكلفة.")
    if not invoice.journal_entry_id or invoice.journal_entry.status != JournalEntry.Status.POSTED:
        raise ValidationError("يجب أن يكون قيد فاتورة المصروف المصدر مرحّلًا قبل الرسملة.")

    allocations = list(document.allocations.select_for_update(of=("self",)).select_related(
        "source_invoice_line__expense_account", "receipt_line__product",
        "receipt_line__movement", "receipt_line__movement__destination_warehouse",
    ).order_by("receipt_line_id", "source_invoice_line_id", "pk"))
    if not allocations:
        raise ValidationError("مستند الرسملة لا يحتوي على بنود.")

    # Match inventory posting lock order: products -> warehouses -> balances -> cost layers -> consumptions.
    receipt_line_ids = sorted({line.receipt_line_id for line in allocations})
    product_ids = sorted({line.receipt_line.product_id for line in allocations})
    warehouse_ids = set(StockCostLayer.objects.filter(
        root_source_line_id__in=receipt_line_ids,
    ).values_list("warehouse_id", flat=True))
    warehouse_ids.update(StockMovementLine.objects.filter(
        pk__in=receipt_line_ids,
    ).values_list("movement__destination_warehouse_id", flat=True))
    warehouse_ids.discard(None)
    list(Product.objects.select_for_update().filter(pk__in=product_ids).order_by("pk"))
    list(Warehouse.objects.select_for_update().filter(pk__in=sorted(warehouse_ids)).order_by("pk"))
    list(StockBalance.objects.select_for_update().filter(
        product_id__in=product_ids, warehouse_id__in=warehouse_ids,
    ).order_by("id"))
    list(StockCostLayer.objects.select_for_update(of=("self",)).filter(
        root_source_line_id__in=receipt_line_ids,
    ).order_by("pk"))
    list(StockCostConsumption.objects.select_for_update(of=("self",)).filter(
        source_layer__root_source_line_id__in=receipt_line_ids,
    ).order_by("pk"))

    if sum((line.amount for line in allocations), Decimal("0")) != document.total_amount:
        raise ValidationError("إجمالي مستند الرسملة لا يطابق مجموع بنوده.")

    for source_line_id in sorted({line.source_invoice_line_id for line in allocations}):
        source_line = PurchaseInvoiceLine.objects.select_for_update(of=("self",)).get(pk=source_line_id)
        nonrecoverable_tax = (
            source_line.tax_amount
            if source_line.tax_code_id and not source_line.tax_code.input_tax_recoverable
            else Decimal("0")
        )
        capitalizable_amount = source_line.net_amount + nonrecoverable_tax
        used = LandedCostAllocation.objects.filter(
            source_invoice_line_id=source_line_id,
        ).exclude(document__status=LandedCostDocument.Status.REVERSED).aggregate(
            total=Sum("amount"),
        )["total"] or Decimal("0")
        if used > capitalizable_amount:
            raise ValidationError("إجمالي الرسملة يتجاوز قيمة بند المصروف والضريبة غير القابلة للخصم.")

    source_branch = Branch.objects.select_for_update(of=("self",)).get(
        pk=invoice.branch_id, company=invoice.company, is_active=True,
    )
    prepared = []
    for allocation in allocations:
        if allocation.source_invoice_line.invoice_id != invoice.pk:
            raise ValidationError("بند المصروف لا يتبع فاتورة المصدر.")
        expense_account = allocation.source_invoice_line.expense_account
        if (
            not expense_account or not expense_account.is_active or expense_account.is_group
            or not expense_account.allow_posting or expense_account.nature != ChartOfAccount.Nature.EXPENSE
        ):
            raise ValidationError("حساب بند المصروف يجب أن يكون حساب مصروف تفصيليًا نشطًا ويسمح بالترحيل.")
        if allocation.receipt_line.movement.company_id != invoice.company_id:
            raise ValidationError("بند الاستلام لا يتبع الشركة.")
        amounts = _allocate_landed_cost_to_stock(allocation)
        if sum((sum(bucket.values(), Decimal("0")) for bucket in amounts.values()), Decimal("0")) != allocation.amount:
            raise ValidationError("توزيع بند الرسملة لا يساوي مبلغ البند.")
        prepared.append((allocation, amounts))

    has_remote_branch = any(
        branch_id != source_branch.pk
        for _, amounts in prepared
        for branch_id in amounts
    )
    clearing = (
        _account(invoice.company, AccountRole.Role.INTER_BRANCH_CLEARING, invoice.activity)
        if has_remote_branch else None
    )
    prepared_accounts = {}
    for allocation, amounts in prepared:
        expense_account = allocation.source_invoice_line.expense_account
        for branch_id, bucket in amounts.items():
            if bucket["inventory"]:
                prepared_accounts["inventory"] = _account(invoice.company, AccountRole.Role.INVENTORY, invoice.activity)
            if bucket["cogs"]:
                prepared_accounts["cogs"] = _account(invoice.company, AccountRole.Role.COGS, invoice.activity)
            if bucket["loss"]:
                prepared_accounts["loss"] = _account(invoice.company, AccountRole.Role.INVENTORY_ADJUSTMENT_LOSS, invoice.activity)
        used_accounts = [expense_account, *prepared_accounts.values()]
        if clearing:
            used_accounts.append(clearing)
        if len({account.pk for account in used_accounts}) != len(used_accounts):
            raise ValidationError("يجب فصل حساب المصروف عن حسابات المخزون والتكلفة والخسائر والتسوية بين الفروع.")

    for allocation, amounts in prepared:
        expense_account = allocation.source_invoice_line.expense_account
        local = amounts.get(source_branch.pk, {
            "inventory": Decimal("0"), "cogs": Decimal("0"), "loss": Decimal("0"),
        })
        remote_total = sum(
            (sum(bucket.values(), Decimal("0")) for branch_id, bucket in amounts.items() if branch_id != source_branch.pk),
            Decimal("0"),
        )
        source_lines = []
        descriptions = {
            "inventory": "رسملة نصيب الفرع من تكلفة الشراء على المخزون",
            "cogs": "تحميل نصيب الفرع من تكلفة الشراء على تكلفة المبيعات",
            "loss": "تحميل نصيب الفرع من تكلفة الشراء على خسائر المخزون",
        }
        for category in ("inventory", "cogs", "loss"):
            value = local[category]
            if value:
                source_lines.append((
                    prepared_accounts[category], descriptions[category], value, Decimal("0"),
                    allocation.source_invoice_line.cost_center,
                ))
        if remote_total:
            source_lines.append((clearing, "تسوية نصيب الفروع الأخرى من تكلفة الشراء", remote_total, Decimal("0")))
        source_lines.append((
            expense_account, "إعادة تصنيف مصروف الشحن/الجمارك إلى تكلفة المخزون والمبيعات",
            Decimal("0"), allocation.amount, allocation.source_invoice_line.cost_center,
        ))
        source_entry = _post_landed_cost_journal(
            invoice.company, source_branch, document.document_date,
            f"PUR-LC-{document.pk}-{allocation.pk}-SRC",
            f"رسملة تكلفة شراء من فاتورة {invoice.invoice_number}",
            source_lines, user,
        )
        allocation.source_journal_entry = source_entry
        allocation.save(update_fields=("source_journal_entry",))

        for branch_id, bucket in sorted(amounts.items()):
            branch = Branch.objects.get(pk=branch_id, company=invoice.company, is_active=True)
            distribution = LandedCostDistribution.objects.create(
                allocation=allocation, branch=branch,
                inventory_amount=bucket["inventory"],
                cogs_amount=bucket["cogs"],
                adjustment_loss_amount=bucket["loss"],
            )
            if branch_id == source_branch.pk:
                distribution.journal_entry = source_entry
            else:
                remote_lines = []
                for category in ("inventory", "cogs", "loss"):
                    value = bucket[category]
                    if value:
                        remote_lines.append((
                            prepared_accounts[category], descriptions[category],
                            value, Decimal("0"), allocation.source_invoice_line.cost_center,
                        ))
                remote_total = sum(bucket.values(), Decimal("0"))
                remote_lines.append((
                    clearing, "مقابل رسملة تكلفة الشراء بين الفروع",
                    Decimal("0"), remote_total,
                ))
                distribution.journal_entry = _post_landed_cost_journal(
                    invoice.company, branch, document.document_date,
                    f"PUR-LC-{document.pk}-{allocation.pk}-{branch.pk}",
                    f"نصيب الفرع من رسملة تكلفة فاتورة {invoice.invoice_number}",
                    remote_lines, user,
                )
            distribution.save(update_fields=("journal_entry",))

    document.status = LandedCostDocument.Status.POSTED
    document.posted_by = user
    document.posted_at = timezone.now()
    document.save(update_fields=("status", "posted_by", "posted_at"))
    AuditEvent.objects.create(
        actor=user, company=invoice.company, branch=source_branch,
        action=AuditEvent.Action.ACTION, resource="purchasing.landed_cost.posted",
        object_id=str(document.pk), http_method="POST", status_code=200, succeeded=True,
        metadata={
            "document_number": document.document_number,
            "source_invoice_id": invoice.pk,
            "total_amount": str(document.total_amount),
            "allocation_count": len(allocations),
            "journal_entry_ids": sorted({
                allocation.source_journal_entry_id
                for allocation, _ in prepared if allocation.source_journal_entry_id
            }),
        },
    )
    return document



@transaction.atomic
def reverse_landed_cost_document(document_id, user, entry_date):
    """Reverse landed-cost journals and stock valuation when no later stock consumption exists."""
    document = LandedCostDocument.objects.select_for_update(of=("self",)).select_related(
        "company", "company__base_currency",
    ).get(pk=document_id)
    if document.status != LandedCostDocument.Status.POSTED:
        raise ValidationError("لا يمكن عكس إلا مستند رسملة تكلفة مرحّل.")
    if document.reversal_journal_entry_id:
        raise ValidationError("تم عكس مستند الرسملة من قبل.")
    if entry_date < document.document_date:
        raise ValidationError("تاريخ عكس الرسملة لا يجوز أن يسبق تاريخ المستند.")

    allocations = list(document.allocations.select_for_update(of=("self",)).select_related(
        "source_invoice_line", "receipt_line__product",
    ).prefetch_related("distributions").order_by("receipt_line_id", "source_invoice_line_id", "pk"))
    if not allocations:
        raise ValidationError("لا توجد بنود رسملة يمكن عكسها.")

    # Lock the same inventory dimensions in the same order used by stock movements.
    receipt_line_ids = sorted({line.receipt_line_id for line in allocations})
    product_ids = sorted({line.receipt_line.product_id for line in allocations})
    warehouse_ids = set(StockCostLayer.objects.filter(
        root_source_line_id__in=receipt_line_ids,
    ).values_list("warehouse_id", flat=True))
    warehouse_ids.update(StockMovementLine.objects.filter(
        pk__in=receipt_line_ids,
    ).values_list("movement__destination_warehouse_id", flat=True))
    warehouse_ids.discard(None)
    list(Product.objects.select_for_update().filter(pk__in=product_ids).order_by("pk"))
    list(Warehouse.objects.select_for_update().filter(pk__in=sorted(warehouse_ids)).order_by("pk"))
    list(StockBalance.objects.select_for_update().filter(
        product_id__in=product_ids, warehouse_id__in=warehouse_ids,
    ).order_by("id"))
    list(StockCostLayer.objects.select_for_update(of=("self",)).filter(
        root_source_line_id__in=receipt_line_ids,
    ).order_by("pk"))
    list(StockCostConsumption.objects.select_for_update(of=("self",)).filter(
        source_layer__root_source_line_id__in=receipt_line_ids,
    ).order_by("pk"))

    for allocation in allocations:
        if not allocation.source_journal_entry_id:
            raise ValidationError("قيد إعادة تصنيف مصروف المصدر غير مرتبط ببند الرسملة.")
        receipt_line = allocation.receipt_line
        if StockCostConsumption.objects.filter(
            source_layer__root_source_line=receipt_line,
            created_at__gt=document.posted_at,
        ).exists():
            raise ValidationError(
                f"لا يمكن عكس الرسملة للصنف {receipt_line.product.code} بعد حدوث استهلاك مخزني لاحق؛ "
                "يلزم معالجة أثر التكلفة اللاحق أولًا."
            )

        layers = list(StockCostLayer.objects.select_for_update(of=("self",)).filter(
            root_source_line=receipt_line,
        ).select_related("warehouse").order_by("pk"))
        quantity = receipt_line.quantity
        if quantity <= 0:
            raise ValidationError("كمية الاستلام الأصلية غير صالحة لعكس الرسملة.")
        unit_addition = (allocation.amount / quantity).quantize(
            Decimal("0.00000001"), rounding=ROUND_HALF_UP,
        )
        for layer in layers:
            if layer.quantity_remaining > 0:
                if layer.unit_cost < unit_addition:
                    raise ValidationError("تعذر عكس زيادة تكلفة طبقة المخزون دون إنتاج تكلفة سالبة.")
                layer.unit_cost = (layer.unit_cost - unit_addition).quantize(
                    Decimal("0.00000001"), rounding=ROUND_HALF_UP,
                )
                layer.save(update_fields=("unit_cost",))

        distributions = list(allocation.distributions.select_for_update(of=("self",)).select_related("branch").order_by("branch_id"))
        inventory_total = sum((dist.inventory_amount for dist in distributions), Decimal("0"))
        remaining_by_balance = defaultdict(Decimal)
        for layer in layers:
            if layer.quantity_remaining > 0:
                key = (layer.warehouse_id, layer.location_id, layer.expiry_date)
                remaining_by_balance[key] += layer.quantity_remaining
        if inventory_total:
            remaining_quantity = sum(remaining_by_balance.values(), Decimal("0"))
            if remaining_quantity <= 0:
                raise ValidationError("قيمة مخزون مرسملة موجودة دون طبقات متبقية لعكسها.")
            currency = document.company.base_currency
            quantum = Decimal("1").scaleb(-currency.decimal_places)
            raw = {
                key: qty * allocation.amount / quantity
                for key, qty in remaining_by_balance.items()
            }
            rounded = {
                key: value.quantize(quantum, rounding=ROUND_HALF_UP)
                for key, value in raw.items()
            }
            residual = inventory_total - sum(rounded.values(), Decimal("0"))
            if residual:
                largest = max(raw, key=raw.get)
                rounded[largest] += residual
            for (warehouse_id, location_id, expiry_date), value in rounded.items():
                balance = StockBalance.objects.select_for_update().get(
                    product_id=receipt_line.product_id,
                    warehouse_id=warehouse_id,
                    location_id=location_id,
                    expiry_date=expiry_date,
                )
                if balance.inventory_value < value:
                    raise ValidationError("قيمة المخزون الحالية أقل من مبلغ الرسملة المطلوب عكسه.")
                balance.inventory_value -= value
                if balance.quantity > 0:
                    balance.average_cost = (balance.inventory_value / balance.quantity).quantize(
                        Decimal("0.000001"), rounding=ROUND_HALF_UP,
                    )
                balance.save(update_fields=("inventory_value", "average_cost", "updated_at"))

    reversal_by_original = {}
    for allocation in allocations:
        original_entries = []
        if allocation.source_journal_entry_id:
            original_entries.append(allocation.source_journal_entry_id)
        original_entries.extend(
            distribution.journal_entry_id
            for distribution in allocation.distributions.all()
            if distribution.journal_entry_id
        )
        for original_id in sorted(set(original_entries)):
            if original_id in reversal_by_original:
                continue
            original = JournalEntry.objects.select_for_update(of=("self",)).get(pk=original_id)
            if original.status != JournalEntry.Status.POSTED:
                raise ValidationError("يوجد قيد رسملة غير مرحّل أو سبق عكسه.")
            try:
                reversal = reverse_operational_journal(
                    original.pk, user, entry_date, JournalEntry.Source.PURCHASES,
                )
            except DjangoValidationError as exc:
                detail = exc.message_dict if hasattr(exc, "message_dict") else exc.messages
                raise ValidationError(detail) from exc
            reversal_by_original[original_id] = reversal

        source_reversal = reversal_by_original[allocation.source_journal_entry_id]
        LandedCostAllocation.objects.filter(pk=allocation.pk).update(
            source_reversal_journal_entry=source_reversal,
        )
        for distribution in allocation.distributions.all():
            if distribution.journal_entry_id:
                LandedCostDistribution.objects.filter(pk=distribution.pk).update(
                    reversal_journal_entry=reversal_by_original[distribution.journal_entry_id],
                )

    first_reversal = next(iter(reversal_by_original.values()))
    LandedCostDocument.objects.filter(pk=document.pk).update(
        status=LandedCostDocument.Status.REVERSED,
        reversal_journal_entry=first_reversal,
        reversed_at=timezone.now(),
    )
    AuditEvent.objects.create(
        actor=user, company=document.company,
        branch=allocations[0].source_journal_entry.branch,
        action=AuditEvent.Action.ACTION, resource="purchasing.landed_cost.reversed",
        object_id=str(document.pk), http_method="POST", status_code=200, succeeded=True,
        metadata={
            "document_number": document.document_number,
            "reversal_date": entry_date.isoformat(),
            "reversal_journal_entry_ids": sorted(
                reversal.pk for reversal in reversal_by_original.values()
            ),
        },
    )
    return LandedCostDocument.objects.get(pk=document.pk)
