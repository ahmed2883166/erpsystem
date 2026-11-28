"""خدمات ترحيل التسليم وفواتير المبيعات وتحصيلات العملاء وربطها بالمخزون والأستاذ العام."""

from decimal import Decimal

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from accounting.models import AccountRole, JournalEntry, JournalLine
from accounting.services import get_account_for_role, post_journal
from companies.access import resolve_warehouse_branch
from companies.models import ExchangeRate, FiscalYear
from inventory.models import StockMovement, StockMovementLine
from inventory.services import post_stock_movement
from approvals.snapshots import invoice_discount_snapshot

from .models import CustomerReceipt, CustomerReceiptBatch, CustomerReceiptBatchAllocation, SalesDelivery, SalesInvoice, SalesInvoiceLine, SalesReturn, SalesOrder, SalesDeliveryAllocation


def _account(company, role):
    """استخراج الحساب التفصيلي المرتبط بدور المبيعات المطلوب."""
    return get_account_for_role(company, role)

def _branch_and_year(delivery, entry_date):
    """تحديد فرع المستند والسنة المالية التي سيُسجل فيها القيد."""
    branch = resolve_warehouse_branch(delivery.company, delivery.warehouse)
    if branch and not branch.is_active:
        raise ValidationError("فرع المخزن موقوف.")
    if not branch:
        raise ValidationError("يجب إنشاء فرع نشط قبل ترحيل المبيعات.")
    year = FiscalYear.objects.filter(
        company=delivery.company, status=FiscalYear.Status.OPEN,
        start_date__lte=entry_date, end_date__gte=entry_date,
    ).order_by("-start_date").first()
    if not year:
        raise ValidationError("لا توجد سنة مالية مفتوحة تشمل تاريخ المستند.")
    return branch, year

def _branch_and_year_for_invoice(invoice, entry_date):
    """Resolve branch for either a stock-backed invoice or a direct service invoice."""
    if invoice.delivery_id:
        return _branch_and_year(invoice.delivery, entry_date)
    branch = invoice.branch
    if not branch or branch.company_id != invoice.company_id or not branch.is_active:
        raise ValidationError("حدد فرعًا نشطًا تابعًا للشركة لفاتورة الخدمة.")
    year = FiscalYear.objects.filter(
        company=invoice.company, status=FiscalYear.Status.OPEN,
        start_date__lte=entry_date, end_date__gte=entry_date,
    ).order_by("-start_date").first()
    if not year:
        raise ValidationError("لا توجد سنة مالية مفتوحة تشمل تاريخ المستند.")
    return branch, year


@transaction.atomic
def post_sales_delivery(delivery_id, user):
    """ترحيل إذن التسليم إلى حركة مخزون وتكلفة مبيعات مرة واحدة."""
    delivery = SalesDelivery.objects.select_for_update().select_related(
        "company", "customer", "warehouse", "stock_movement"
    ).get(pk=delivery_id)
    if delivery.stock_movement.status != StockMovement.Status.DRAFT:
        raise ValidationError("إذن التسليم مرحّل بالفعل.")
    if not delivery.customer.is_active or delivery.customer.company_id != delivery.company_id:
        raise ValidationError("العميل موقوف أو لا يتبع الشركة.")
    if not delivery.warehouse.is_active or delivery.warehouse.company_id != delivery.company_id:
        raise ValidationError("المخزن موقوف أو لا يتبع الشركة.")
    post_stock_movement(delivery.stock_movement_id, user)
    if delivery.order_id:
        order = SalesOrder.objects.select_for_update().get(pk=delivery.order_id)
        complete = True
        for order_line in order.lines.all():
            posted_quantity = SalesDeliveryAllocation.objects.filter(
                order_line=order_line,
                delivery__stock_movement__status=StockMovement.Status.POSTED,
            ).aggregate(total=Sum("quantity"))["total"] or Decimal("0")
            if posted_quantity < order_line.quantity:
                complete = False
                break
        order.status = SalesOrder.Status.DELIVERED if complete else SalesOrder.Status.PARTIALLY_DELIVERED
        order.save(update_fields=("status", "updated_at"))
    return SalesDelivery.objects.select_related("stock_movement", "order").get(pk=delivery.pk)


@transaction.atomic
def post_sales_invoice(invoice_id, user):
    """ترحيل فاتورة المبيعات والإيراد والضريبة وذمم العميل."""
    invoice = SalesInvoice.objects.select_for_update().get(pk=invoice_id)
    if invoice.status != SalesInvoice.Status.DRAFT:
        raise ValidationError("فاتورة المبيعات مرحلة بالفعل.")
    discount_exceeded = (
        invoice.discount_approval_limit is not None
        and invoice.lines.filter(discount_percent__gt=invoice.discount_approval_limit).exists()
    )
    amount_exceeded = (
        invoice.invoice_approval_limit is not None
        and invoice.grand_total > invoice.invoice_approval_limit
    )
    if discount_exceeded or amount_exceeded:
        from approvals.models import SalesInvoiceApproval
        approval = invoice.approval_requests.order_by("-requested_at", "-pk").first()
        current_max_discount = max(
            invoice.lines.values_list("discount_percent", flat=True),
            default=Decimal("0"),
        )
        if (
            not approval
            or approval.status != SalesInvoiceApproval.Status.APPROVED
            or approval.requested_discount_total != invoice.discount_total
            or approval.requested_max_discount_percent != current_max_discount
            or approval.requested_invoice_limit != invoice.invoice_approval_limit
            or approval.requested_lines_snapshot != invoice_discount_snapshot(invoice)
        ):
            raise ValidationError("تجاوزت الفاتورة حد الخصم أو قيمة الفاتورة المعتمد؛ يلزم اعتماد مدير الشركة لآخر نسخة قبل الترحيل.")
    delivery = invoice.delivery
    lines = list(invoice.lines.select_related("delivery_line", "product"))
    if not lines:
        raise ValidationError("لا توجد بنود في فاتورة المبيعات.")
    if delivery:
        if invoice.invoice_date < delivery.delivery_date:
            raise ValidationError("تاريخ الفاتورة لا يجوز أن يسبق تاريخ إذن التسليم.")
        if delivery.stock_movement.status != StockMovement.Status.POSTED:
            raise ValidationError("لا يمكن ترحيل الفاتورة قبل ترحيل إذن التسليم.")
        expected_ids = set(delivery.stock_movement.lines.values_list("id", flat=True))
        delivery_line_ids = [line.delivery_line_id for line in lines]
        if (
            any(not line_id for line_id in delivery_line_ids)
            or len(delivery_line_ids) != len(set(delivery_line_ids))
            or not set(delivery_line_ids).issubset(expected_ids)
        ):
            raise ValidationError("بنود الفاتورة يجب أن تكون بنودًا صحيحة وغير مكررة من إذن التسليم.")
        locked_delivery_lines = {
            row.pk: row
            for row in StockMovementLine.objects.select_for_update()
            .filter(pk__in=delivery_line_ids, movement_id=delivery.stock_movement_id)
            .order_by("pk")
        }
        if len(locked_delivery_lines) != len(set(delivery_line_ids)):
            raise ValidationError("بعض بنود الفاتورة لا تتبع إذن التسليم المحدد.")
        for line in lines:
            delivery_line = locked_delivery_lines[line.delivery_line_id]
            if line.product.product_type != "goods" or line.product_id != delivery_line.product_id:
                raise ValidationError("فاتورة التسليم يجب أن تحتوي على أصناف مخزنية مطابقة للإذن.")
            if line.quantity <= 0 or line.quantity > delivery_line.quantity:
                raise ValidationError("كمية الفاتورة لا يجوز أن تتجاوز كمية بند إذن التسليم.")
            previously_reserved = SalesInvoiceLine.objects.filter(
                delivery_line_id=delivery_line.pk
            ).exclude(invoice_id=invoice.pk).aggregate(total=Sum("quantity"))["total"] or Decimal("0")
            if previously_reserved + line.quantity > delivery_line.quantity:
                raise ValidationError("إجمالي الكميات المفوترة أو المحجوزة يتجاوز كمية إذن التسليم.")
    else:
        if not invoice.branch_id or any(
            line.delivery_line_id or line.product.product_type != "service"
            for line in lines
        ):
            raise ValidationError("فاتورة الخدمة يجب أن تحتوي على خدمات فقط وترتبط بفرع نشط.")
    net = sum((line.net_amount for line in lines), Decimal("0")).quantize(Decimal("0.0001"))
    tax = sum((line.tax_amount for line in lines), Decimal("0")).quantize(Decimal("0.0001"))
    total = net + tax
    if invoice.transaction_currency_id:
        from accounting.fx_arap import validate_snapshot
        approved = ExchangeRate.objects.filter(
            company=invoice.company, currency=invoice.transaction_currency,
            rate_date=invoice.exchange_rate_date,
        ).first()
        if not approved:
            raise ValidationError("لا يوجد سعر صرف معتمد بتاريخ الفاتورة.")
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
            raise ValidationError("تعذر التحقق من سعر الصرف لفاتورة المبيعات؛ راجع بيانات العملة والسعر المعتمد.") from None
        if abs(expected_base - total) > Decimal("0.0002") * max(len(lines), 1):
            raise ValidationError("قيمة الفاتورة بالعملة الأساسية لا تطابق إجماليها الأجنبي وسعر الصرف.")
        invoice.transaction_net_total = foreign_net
        invoice.transaction_tax_total = foreign_tax
        invoice.transaction_grand_total = foreign_total
    else:
        if any(line.transaction_unit_price is not None for line in lines):
            raise ValidationError("توجد أسعار أجنبية دون تحديد عملة الفاتورة.")
    receivable = _account(invoice.company, AccountRole.Role.ACCOUNTS_RECEIVABLE)
    sales = _account(invoice.company, AccountRole.Role.SALES)
    vat_output = _account(invoice.company, AccountRole.Role.VAT_OUTPUT) if tax else None
    roles = [receivable, sales] + ([vat_output] if vat_output else [])
    if len({x.pk for x in roles}) != len(roles):
        raise ValidationError("يجب ربط حسابات العملاء والمبيعات وضريبة المخرجات بحسابات مختلفة.")
    branch, year = _branch_and_year_for_invoice(invoice, invoice.invoice_date)
    entry = JournalEntry.objects.create(
        company=invoice.company, branch=branch, fiscal_year=year,
        entry_number=f"SALE-INV-{invoice.pk}", entry_date=invoice.invoice_date,
        description=f"فاتورة مبيعات العميل {invoice.customer.name_ar} رقم {invoice.invoice_number}"[:500],
        source_type=JournalEntry.Source.SALES, created_by=user,
    )
    journal_lines = [
        JournalLine(entry=entry, account=receivable, description="إثبات مستحقات العميل", debit=total, credit=0),
        JournalLine(entry=entry, account=sales, description="إثبات إيراد المبيعات", debit=0, credit=net),
    ]
    if vat_output and tax:
        journal_lines.append(JournalLine(entry=entry, account=vat_output, description="ضريبة قيمة مضافة مخرجات", debit=0, credit=tax))
    JournalLine.objects.bulk_create(journal_lines)
    try:
        posted = post_journal(entry.pk, actor=user)
    except DjangoValidationError as exc:
        detail = exc.message_dict if hasattr(exc, "message_dict") else exc.messages
        raise ValidationError(detail) from exc
    invoice.net_total, invoice.tax_total, invoice.grand_total = net, tax, total
    invoice.journal_entry = posted
    invoice.status = SalesInvoice.Status.POSTED
    invoice.posted_at = timezone.now()
    invoice.save(update_fields=("net_total", "tax_total", "grand_total", "journal_entry", "status", "posted_at"))
    return invoice


@transaction.atomic
def post_customer_receipt(receipt_id, user):
    """ترحيل تحصيل العميل مع تخصيص القيمة الدفترية وفروق العملة المحققة."""
    from accounting.fx_arap import settle_foreign_balance, validate_snapshot

    receipt = CustomerReceipt.objects.select_for_update().get(pk=receipt_id)
    if receipt.status != CustomerReceipt.Status.DRAFT:
        raise ValidationError("سند القبض مرحّل بالفعل.")
    invoice = SalesInvoice.objects.select_for_update().get(pk=receipt.invoice_id)
    if invoice.status != SalesInvoice.Status.POSTED:
        raise ValidationError("لا يمكن قبض فاتورة غير مرحلة.")
    if receipt.receipt_date < invoice.invoice_date:
        raise ValidationError("تاريخ سند القبض لا يجوز أن يسبق تاريخ فاتورة المبيعات.")
    if receipt.company_id != invoice.company_id or receipt.customer_id != invoice.customer_id:
        raise ValidationError("بيانات السند لا تطابق فاتورة العميل.")
    if CustomerReceipt.objects.filter(
        invoice=invoice, status=CustomerReceipt.Status.POSTED,
    ).filter(
        Q(receipt_date__gt=receipt.receipt_date)
        | Q(receipt_date=receipt.receipt_date, pk__gt=receipt.pk)
    ).exists():
        raise ValidationError("لا يمكن ترحيل سند قبض بتاريخ يسبق سندًا مرحّلًا أحدث؛ عالج المستندات بترتيبها الزمني.")

    if invoice.transaction_currency_id:
        if receipt.transaction_currency_id != invoice.transaction_currency_id:
            raise ValidationError("عملة التحصيل يجب أن تطابق عملة فاتورة العميل.")
        approved = ExchangeRate.objects.filter(
            company=receipt.company, currency=receipt.transaction_currency,
            rate_date=receipt.exchange_rate_date,
        ).first()
        if not approved:
            raise ValidationError("لا يوجد سعر صرف معتمد بتاريخ التحصيل.")
        try:
            expected_amount = validate_snapshot(
                base_currency_code=receipt.company.base_currency.code,
                currency_code=receipt.transaction_currency.code,
                foreign_amount=receipt.transaction_amount, rate=receipt.exchange_rate,
                rate_date=receipt.exchange_rate_date, document_date=receipt.receipt_date,
                approved_rate=approved.rate,
            )
        except (ValueError, TypeError):
            raise ValidationError("تعذر التحقق من سعر الصرف لسند تحصيل العميل.") from None
        if expected_amount != receipt.amount:
            raise ValidationError("مبلغ التحصيل الأساسي لا يطابق المبلغ الأجنبي مضروبًا في سعر الصرف.")
        from accounting.fx_settlement import allocate_carrying_base_amount, calculate_realized_fx
        from accounting.fx_subledger_services import open_fx_invoice_balances
        from accounting.models import FxSubledgerRevaluation
        if CustomerReceipt.objects.filter(
            invoice=invoice, status=CustomerReceipt.Status.POSTED,
            receipt_date__gt=receipt.receipt_date,
        ).exists() or FxSubledgerRevaluation.objects.filter(
            sales_invoice=invoice, revaluation_date__gte=receipt.receipt_date,
        ).filter(
            Q(journal_entry__isnull=True)
            | ~Q(journal_entry__status=JournalEntry.Status.REVERSED)
        ).exists():
            raise ValidationError("لا يمكن ترحيل تحصيل بتاريخ يسبق حركة تحصيل أو إعادة تقييم مرحّلة.")
        remaining_foreign, historical_remaining, current_remaining = open_fx_invoice_balances(
            invoice, direction="receivable", as_of=receipt.receipt_date,
        )
        try:
            settlement = settle_foreign_balance(
                direction="receivable", remaining_foreign_amount=remaining_foreign,
                remaining_carrying_base_amount=current_remaining,
                settled_foreign_amount=receipt.transaction_amount,
                settlement_rate=receipt.exchange_rate,
            )
            historical_carrying = allocate_carrying_base_amount(
                remaining_foreign, historical_remaining, receipt.transaction_amount,
            )
            realized_gain, realized_loss = calculate_realized_fx(
                "receivable", historical_carrying, settlement.settlement_base_amount,
            )
        except ValueError:
            raise ValidationError("تعذر احتساب تسوية العملة لتحصيل العميل.") from None
        carrying_amount = settlement.carrying_base_amount
        unrealized_adjustment = carrying_amount - historical_carrying
    else:
        if any(value is not None for value in (
            receipt.transaction_currency_id, receipt.transaction_amount,
            receipt.exchange_rate, receipt.exchange_rate_date,
        )):
            raise ValidationError("لا يمكن استخدام بيانات عملة أجنبية مع فاتورة بالعملة الأساسية.")
        paid = CustomerReceipt.objects.filter(
            invoice=invoice, status=CustomerReceipt.Status.POSTED,
            receipt_date__lte=receipt.receipt_date,
        ).exclude(pk=receipt.pk).aggregate(total=Sum("amount"))["total"] or Decimal("0")
        batch_paid = CustomerReceiptBatchAllocation.objects.filter(
            invoice=invoice, allocation_date__lte=receipt.receipt_date,
        ).filter(
            Q(batch__status=CustomerReceiptBatch.Status.POSTED)
            | Q(batch__status=CustomerReceiptBatch.Status.REVERSED, batch__reversal_date__gt=receipt.receipt_date)
        ).aggregate(total=Sum("amount"))["total"] or Decimal("0")
        returned = SalesReturn.objects.filter(
            invoice=invoice, return_date__lte=receipt.receipt_date,
        ).filter(
            Q(status=SalesReturn.Status.POSTED)
            | Q(status=SalesReturn.Status.REVERSED, reversal_date__gt=receipt.receipt_date)
        ).aggregate(total=Sum("grand_total"))["total"] or Decimal("0")
        remaining = max(Decimal("0"), invoice.grand_total - paid - batch_paid - returned)
        if receipt.amount > remaining:
            raise ValidationError(f"قيمة السند تتجاوز المتبقي بعد خصم مردودات المبيعات ({remaining}).")
        carrying_amount = receipt.amount
        historical_carrying = receipt.amount
        unrealized_adjustment = Decimal("0")
        realized_gain = realized_loss = Decimal("0")

    receivable = _account(receipt.company, AccountRole.Role.ACCOUNTS_RECEIVABLE)
    cash_role = AccountRole.Role.CASH if receipt.method == CustomerReceipt.Method.CASH else AccountRole.Role.BANK
    cash = receipt.payment_account or _account(receipt.company, cash_role)
    if cash.company_id != receipt.company_id or cash.nature != "asset" or not cash.is_active or cash.is_group or not cash.allow_posting:
        raise ValidationError("حساب التحصيل يجب أن يكون حساب خزينة/بنك نشطًا وقابلًا للترحيل تابعًا للشركة.")
    branch, year = _branch_and_year_for_invoice(invoice, receipt.receipt_date)
    if receipt.payment_account_id:
        from treasury.models import TreasuryAccount
        expected_kind = TreasuryAccount.Kind.CASH if receipt.method == CustomerReceipt.Method.CASH else TreasuryAccount.Kind.BANK
        treasury_account = TreasuryAccount.objects.filter(company=receipt.company, account=cash, kind=expected_kind, is_active=True).first()
        if (not treasury_account or (treasury_account.branch_id and treasury_account.branch_id != branch.pk)
                or treasury_account.currency_code != receipt.company.base_currency.code):
            raise ValidationError("حساب التحصيل المختار يجب أن يكون بعملة الشركة الأساسية ومطابقًا للفرع والطريقة.")
    if receivable.pk == cash.pk:
        raise ValidationError("حساب العملاء وحساب الصندوق/البنك يجب أن يكونا مختلفين.")

    journal_lines = [
        JournalLine(account=cash, description="استلام نقدية/تحويل من العميل", debit=receipt.amount, credit=0),
        JournalLine(account=receivable, description="تخفيض مستحقات العميل بالقيمة الدفترية", debit=0, credit=carrying_amount),
    ]
    gain = _account(receipt.company, AccountRole.Role.REALIZED_FX_GAIN) if realized_gain else None
    loss = _account(receipt.company, AccountRole.Role.REALIZED_FX_LOSS) if realized_loss else None
    unrealized_gain = _account(receipt.company, AccountRole.Role.UNREALIZED_FX_GAIN) if unrealized_adjustment > 0 else None
    unrealized_loss = _account(receipt.company, AccountRole.Role.UNREALIZED_FX_LOSS) if unrealized_adjustment < 0 else None
    used_ids = {cash.pk, receivable.pk}
    for account in (gain, loss, unrealized_gain, unrealized_loss):
        if account and account.pk in used_ids:
            raise ValidationError("حسابات فروق العملة يجب أن تكون مستقلة عن النقدية والعملاء.")
        if account:
            used_ids.add(account.pk)
    if realized_gain:
        journal_lines.append(JournalLine(account=gain, description="أرباح فروق عملة محققة على أساس التكلفة التاريخية", debit=0, credit=realized_gain))
    if realized_loss:
        journal_lines.append(JournalLine(account=loss, description="خسائر فروق عملة محققة على أساس التكلفة التاريخية", debit=realized_loss, credit=0))
    if unrealized_adjustment > 0:
        journal_lines.append(JournalLine(account=unrealized_gain, description="عكس ربح إعادة التقييم غير المحقق عند التحصيل", debit=unrealized_adjustment, credit=0))
    elif unrealized_adjustment < 0:
        journal_lines.append(JournalLine(account=unrealized_loss, description="عكس خسارة إعادة التقييم غير المحققة عند التحصيل", debit=0, credit=-unrealized_adjustment))

    entry = JournalEntry.objects.create(
        company=receipt.company, branch=branch, fiscal_year=year,
        entry_number=f"SALE-REC-{receipt.pk}", entry_date=receipt.receipt_date,
        description=f"سند قبض العميل {invoice.customer.name_ar} رقم {receipt.receipt_number}"[:500],
        source_type=JournalEntry.Source.SALES, created_by=user,
    )
    for line in journal_lines:
        line.entry = entry
    JournalLine.objects.bulk_create(journal_lines)
    try:
        posted = post_journal(entry.pk, actor=user)
    except DjangoValidationError as exc:
        detail = exc.message_dict if hasattr(exc, "message_dict") else exc.messages
        raise ValidationError(detail) from exc
    if receipt.method == CustomerReceipt.Method.BANK:
        from treasury.models import TreasuryTransaction
        from treasury.services import record_posted_document_bank_movement
        record_posted_document_bank_movement(
            company=receipt.company, branch=branch, fiscal_year=year, account=cash,
            kind=TreasuryTransaction.Kind.RECEIPT, amount=receipt.amount,
            transaction_date=receipt.receipt_date, document_number=f"AUTO-SALE-REC-{receipt.pk}",
            description=f"تحصيل فاتورة مبيعات {invoice.invoice_number} - سند {receipt.receipt_number}",
            counter_account=receivable, journal_entry=posted, user=user, reference=receipt.reference,
        )
    receipt.carrying_base_amount = carrying_amount
    receipt.historical_carrying_base_amount = historical_carrying
    receipt.realized_fx_gain = realized_gain
    receipt.realized_fx_loss = realized_loss
    receipt.journal_entry = posted
    receipt.status = CustomerReceipt.Status.POSTED
    receipt.posted_at = timezone.now()
    receipt.save(update_fields=("carrying_base_amount", "historical_carrying_base_amount", "realized_fx_gain", "realized_fx_loss", "journal_entry", "status", "posted_at"))
    return receipt
