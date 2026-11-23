"""خدمات ترحيل التسليم وفواتير المبيعات وتحصيلات العملاء وربطها بالمخزون والأستاذ العام."""

from decimal import Decimal

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from accounting.models import AccountRole, JournalEntry, JournalLine
from accounting.services import get_account_for_role, post_journal
from companies.access import resolve_warehouse_branch
from companies.models import ExchangeRate, FiscalYear
from inventory.models import StockMovement
from inventory.services import post_stock_movement
from approvals.snapshots import invoice_discount_snapshot

from .models import CustomerReceipt, SalesDelivery, SalesInvoice


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
    return SalesDelivery.objects.select_related("stock_movement").get(pk=delivery.pk)


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
    if delivery.stock_movement.status != StockMovement.Status.POSTED:
        raise ValidationError("لا يمكن ترحيل الفاتورة قبل ترحيل إذن التسليم.")
    lines = list(invoice.lines.select_related("delivery_line", "delivery_line__product"))
    expected = list(delivery.stock_movement.lines.all())
    if not lines or len(lines) != len(expected) or {x.delivery_line_id for x in lines} != {x.pk for x in expected}:
        raise ValidationError("أصناف الفاتورة لا تطابق إذن التسليم.")
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
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
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
    branch, year = _branch_and_year(delivery, invoice.invoice_date)
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
        posted = post_journal(entry.pk)
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
    if receipt.company_id != invoice.company_id or receipt.customer_id != invoice.customer_id:
        raise ValidationError("بيانات السند لا تطابق فاتورة العميل.")

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
        except (ValueError, TypeError) as exc:
            raise ValidationError(str(exc)) from exc
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
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        carrying_amount = settlement.carrying_base_amount
        unrealized_adjustment = carrying_amount - historical_carrying
    else:
        if any(value is not None for value in (
            receipt.transaction_currency_id, receipt.transaction_amount,
            receipt.exchange_rate, receipt.exchange_rate_date,
        )):
            raise ValidationError("لا يمكن استخدام بيانات عملة أجنبية مع فاتورة بالعملة الأساسية.")
        paid = CustomerReceipt.objects.filter(
            invoice=invoice, status=CustomerReceipt.Status.POSTED
        ).exclude(pk=receipt.pk).aggregate(total=Sum("amount"))["total"] or Decimal("0")
        if paid + receipt.amount > invoice.grand_total:
            remaining = max(Decimal("0"), invoice.grand_total - paid)
            raise ValidationError(f"قيمة السند تتجاوز المتبقي على الفاتورة ({remaining}).")
        carrying_amount = receipt.amount
        historical_carrying = receipt.amount
        unrealized_adjustment = Decimal("0")
        realized_gain = realized_loss = Decimal("0")

    receivable = _account(receipt.company, AccountRole.Role.ACCOUNTS_RECEIVABLE)
    cash_role = AccountRole.Role.CASH if receipt.method == CustomerReceipt.Method.CASH else AccountRole.Role.BANK
    cash = receipt.payment_account or _account(receipt.company, cash_role)
    if cash.company_id != receipt.company_id or cash.nature != "asset" or not cash.is_active or cash.is_group or not cash.allow_posting:
        raise ValidationError("حساب التحصيل يجب أن يكون حساب خزينة/بنك نشطًا وقابلًا للترحيل تابعًا للشركة.")
    branch, year = _branch_and_year(invoice.delivery, receipt.receipt_date)
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
        posted = post_journal(entry.pk)
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
