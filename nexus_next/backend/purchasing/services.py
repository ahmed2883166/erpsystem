"""خدمات دورة المشتريات وترحيل الاستلام وفواتير الموردين ومدفوعاتهم."""

from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import F, Sum
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from accounting.models import AccountRole, JournalEntry, JournalLine
from accounting.services import get_account_for_role, post_journal
from companies.models import Branch, ExchangeRate, FiscalYear
from inventory.models import StockBalance, StockCostConsumption, StockCostLayer, StockMovement
from inventory.services import post_stock_movement

from .models import PurchaseInvoice, PurchaseInvoiceTaxAllocation, PurchaseOrder, PurchaseOrderLine, PurchaseOrderReceiptLine, PurchaseReceipt, SupplierReturn, SupplierReturnLine


def _account(company, role):
    """استخراج الحساب التفصيلي المرتبط بدور المشتريات المطلوب."""
    return get_account_for_role(company, role)


def _branch_for_stock_warehouse(company, warehouse):
    """تحديد الفرع الذي يحمل قيمة المخزون أو تكلفة الصرف."""
    branch = warehouse.branch if warehouse.branch_id else None
    if branch and not branch.is_active:
        raise ValidationError("لا يمكن توزيع تكلفة الضريبة على فرع مخزني موقوف.")
    if not branch:
        branch = Branch.objects.filter(company=company, is_active=True, is_head_office=True).first()
        if not branch:
            branch = Branch.objects.filter(company=company, is_active=True).order_by("code").first()
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
        if movement.movement_type == StockMovement.MovementType.ISSUE:
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


def _branch_and_year(receipt, entry_date):
    """تحديد فرع المستند والسنة المالية التي سيُسجل فيها القيد."""
    warehouse = receipt.warehouse
    branch = warehouse.branch if warehouse.branch_id else None
    if branch and not branch.is_active:
        raise ValidationError("فرع المخزن موقوف.")
    if not branch:
        branch = Branch.objects.filter(company=receipt.company, is_active=True, is_head_office=True).first()
        if not branch:
            branch = Branch.objects.filter(company=receipt.company, is_active=True).order_by("code").first()
    if not branch:
        raise ValidationError("يجب إنشاء فرع نشط قبل ترحيل المشتريات.")
    year = FiscalYear.objects.filter(
        company=receipt.company, status=FiscalYear.Status.OPEN,
        start_date__lte=entry_date, end_date__gte=entry_date,
    ).order_by("-start_date").first()
    if not year:
        raise ValidationError("لا توجد سنة مالية مفتوحة تشمل تاريخ إذن الاستلام.")
    return branch, year


@transaction.atomic
def post_purchase_receipt(receipt_id, user):
    """ترحيل استلام المشتريات إلى المخزون وحساب البضاعة المستلمة غير المفوترة."""
    receipt = PurchaseReceipt.objects.select_for_update().select_related(
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
    return PurchaseReceipt.objects.select_related("stock_movement").get(pk=receipt.pk)


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
    receipt = invoice.receipt
    if receipt.stock_movement.status != StockMovement.Status.POSTED:
        raise ValidationError("لا يمكن ترحيل الفاتورة قبل ترحيل إذن الاستلام.")
    lines = list(invoice.lines.select_related("receipt_line", "receipt_line__product", "tax_code"))
    expected = list(receipt.stock_movement.lines.all())
    if len(lines) != len(expected) or {x.receipt_line_id for x in lines} != {x.pk for x in expected}:
        raise ValidationError("أصناف الفاتورة لا تطابق إذن الاستلام.")
    grni_amount = sum((line.cost_amount for line in expected), Decimal("0")).quantize(Decimal("0.0001"))
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
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
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
        for line in lines:
            if line.tax_code_id and not line.tax_code.input_tax_recoverable and line.tax_amount:
                line_allocations = _capitalize_nonrecoverable_stock_tax(line)
                for branch_id, amounts in line_allocations.items():
                    for category, amount in amounts.items():
                        allocations_by_branch[branch_id][category] += amount

    payable = _account(invoice.company, AccountRole.Role.ACCOUNTS_PAYABLE)
    grni = _account(invoice.company, AccountRole.Role.GOODS_RECEIVED_NOT_INVOICED)
    vat_input = _account(invoice.company, AccountRole.Role.VAT_INPUT) if recoverable_tax > 0 else None
    clearing = _account(invoice.company, AccountRole.Role.INTER_BRANCH_CLEARING) if nonrecoverable_tax > 0 else None
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
        allocation_accounts["inventory"] = _account(invoice.company, AccountRole.Role.INVENTORY)
    if allocation_totals["cogs"]:
        allocation_accounts["cogs"] = _account(invoice.company, AccountRole.Role.COGS)
    if allocation_totals["loss"]:
        allocation_accounts["loss"] = _account(invoice.company, AccountRole.Role.INVENTORY_ADJUSTMENT_LOSS)
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
        posted_entry = post_journal(entry.pk)
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
            allocation_entry = post_journal(allocation_entry.pk)
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
        except (ValueError, TypeError) as exc:
            raise ValidationError(str(exc)) from exc
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
        ).exists():
            raise ValidationError("لا يمكن ترحيل سداد بتاريخ يسبق حركة سداد أو إعادة تقييم مرحّلة.")
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
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
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
        if paid_before + payment.amount > invoice.grand_total:
            remaining = max(Decimal("0"), invoice.grand_total - paid_before)
            raise ValidationError(f"قيمة السند تتجاوز المتبقي على الفاتورة ({remaining}).")
        carrying_amount = payment.amount
        historical_carrying = payment.amount
        unrealized_adjustment = Decimal("0")
        realized_gain = realized_loss = Decimal("0")

    receipt = invoice.receipt
    branch, year = _branch_and_year(receipt, payment.payment_date)
    payable = _account(payment.company, AccountRole.Role.ACCOUNTS_PAYABLE)
    cash_role = AccountRole.Role.CASH if payment.method == SupplierPayment.Method.CASH else AccountRole.Role.BANK
    payment_account = payment.payment_account or _account(payment.company, cash_role)
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

    journal_lines = [
        JournalLine(account=payable, description="تخفيض مستحقات المورد بالقيمة الدفترية", debit=carrying_amount, credit=0),
        JournalLine(account=payment_account, description="صرف نقدي/بنكي للمورد", debit=0, credit=payment.amount),
    ]
    gain = _account(payment.company, AccountRole.Role.REALIZED_FX_GAIN) if realized_gain else None
    loss = _account(payment.company, AccountRole.Role.REALIZED_FX_LOSS) if realized_loss else None
    unrealized_gain = _account(payment.company, AccountRole.Role.UNREALIZED_FX_GAIN) if unrealized_adjustment < 0 else None
    unrealized_loss = _account(payment.company, AccountRole.Role.UNREALIZED_FX_LOSS) if unrealized_adjustment > 0 else None
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
        posted_entry = post_journal(entry.pk)
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
def post_supplier_return(return_id, user):
    """ترحيل مرتجع المورد من مصدر الاستلام الأصلي مع تسوية المورد والضريبة والمخزون."""
    document = SupplierReturn.objects.select_for_update(of=("self",)).select_related(
        "company", "supplier", "invoice", "invoice__receipt", "invoice__receipt__warehouse",
    ).get(pk=return_id)
    if document.status != SupplierReturn.Status.DRAFT:
        raise ValidationError("مرتجع المورد مرحّل بالفعل.")
    invoice = PurchaseInvoice.objects.select_for_update().select_related(
        "company", "supplier", "receipt", "receipt__warehouse",
    ).get(pk=document.invoice_id)
    if invoice.status != PurchaseInvoice.Status.POSTED:
        raise ValidationError("لا يمكن ترحيل مرتجع قبل ترحيل فاتورة المورد.")
    if invoice.transaction_currency_id:
        raise ValidationError("مرتجعات الفواتير الأجنبية موقوفة مؤقتًا حتى استكمال معالجة فروق العملة.")
    if document.return_date < invoice.invoice_date:
        raise ValidationError("تاريخ المرتجع لا يجوز أن يسبق تاريخ فاتورة المورد.")
    if document.company_id != invoice.company_id or document.supplier_id != invoice.supplier_id:
        raise ValidationError("بيانات المرتجع لا تطابق فاتورة المورد.")
    lines = list(document.lines.select_for_update().select_related(
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
    movement = document.stock_movement
    if movement.status != StockMovement.Status.DRAFT or movement.movement_type != StockMovement.MovementType.SUPPLIER_RETURN:
        raise ValidationError("حركة المخزون المرتبطة بالمرتجع غير صالحة أو سبق ترحيلها.")
    if movement.source_warehouse_id != invoice.receipt.warehouse_id:
        raise ValidationError("مخزن المرتجع يجب أن يطابق مخزن إذن الاستلام الأصلي في هذه المرحلة.")

    for line in lines:
        invoice_line = line.invoice_line
        receipt_line = invoice_line.receipt_line
        already_returned = returned_by_invoice_line.get(invoice_line.pk, Decimal("0"))
        if already_returned + line.quantity > receipt_line.quantity:
            raise ValidationError(f"كمية المرتجع تتجاوز الكمية المفوترة للصنف {receipt_line.product.code}.")
        if line.movement_line.product_id != receipt_line.product_id or line.movement_line.quantity != line.quantity:
            raise ValidationError("كمية أو صنف حركة المرتجع لا يطابق بند فاتورة المورد.")
        if line.movement_line.source_location_id != receipt_line.destination_location_id:
            raise ValidationError("موقع الصرف يجب أن يطابق موقع استلام الصنف الأصلي.")
        if line.movement_line.expiry_date != receipt_line.expiry_date:
            raise ValidationError("تاريخ صلاحية المرتجع يجب أن يطابق دفعة الاستلام الأصلية.")

        ratio = line.quantity / receipt_line.quantity
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

    # The stock service consumes only layers whose root is the exact receipt line.
    movement = post_stock_movement(movement.pk, user)
    for line in lines:
        expected_inventory_credit = line.net_amount + line.nonrecoverable_tax_amount
        actual_cost = line.movement_line.cost_amount
        if abs(actual_cost - expected_inventory_credit) > Decimal("0.0001"):
            raise ValidationError(
                f"تكلفة الصنف المرتجع ({actual_cost}) لا تطابق قيمة المخزون الدفترية "
                f"({expected_inventory_credit}); تم إلغاء الترحيل دون إنشاء قيد."
            )
        inventory_credit += actual_cost

    grand_total = net_total + tax_total
    paid_total = SupplierPayment.objects.filter(
        invoice=invoice, status=SupplierPayment.Status.POSTED,
    ).aggregate(total=Sum("amount"))["total"] or Decimal("0")
    prior_returns = SupplierReturn.objects.filter(
        invoice=invoice, status=SupplierReturn.Status.POSTED,
    ).exclude(pk=document.pk).aggregate(total=Sum("grand_total"))["total"] or Decimal("0")
    open_payable = invoice.grand_total - paid_total - prior_returns
    if grand_total > open_payable:
        raise ValidationError(
            f"قيمة المرتجع تتجاوز الرصيد المفتوح للمورد ({max(open_payable, Decimal('0'))}). "
            "لا توجد معالجة مردودات نقدية/أرصدة دائنة زائدة في هذا الإصدار."
        )
    if inventory_credit != net_total + (tax_total - recoverable_total):
        raise ValidationError("قيمة تكلفة المخزون المرتجع لا تتطابق مع صافي المرتجع والضريبة غير القابلة للخصم.")

    payable = _account(invoice.company, AccountRole.Role.ACCOUNTS_PAYABLE)
    inventory = _account(invoice.company, AccountRole.Role.INVENTORY)
    vat_input = _account(invoice.company, AccountRole.Role.VAT_INPUT) if recoverable_total else None
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
        posted_entry = post_journal(entry.pk)
    except DjangoValidationError as exc:
        detail = exc.message_dict if hasattr(exc, "message_dict") else exc.messages
        raise ValidationError(detail) from exc

    document.net_amount = net_total
    document.tax_amount = tax_total
    document.grand_total = grand_total
    document.journal_entry = posted_entry
    document.status = SupplierReturn.Status.POSTED
    document.posted_at = timezone.now()
    document.save(update_fields=(
        "net_amount", "tax_amount", "grand_total", "journal_entry", "status", "posted_at",
    ))
    return document
