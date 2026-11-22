"""خدمات دورة المشتريات وترحيل الاستلام وفواتير الموردين ومدفوعاتهم."""

from decimal import Decimal

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import F, Sum
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from accounting.models import AccountRole, JournalEntry, JournalLine
from accounting.services import get_account_for_role, post_journal
from companies.models import Branch, ExchangeRate, FiscalYear
from inventory.models import StockMovement
from inventory.services import post_stock_movement

from .models import PurchaseInvoice, PurchaseOrder, PurchaseOrderLine, PurchaseOrderReceiptLine, PurchaseReceipt


def _account(company, role):
    """استخراج الحساب التفصيلي المرتبط بدور المشتريات المطلوب."""
    return get_account_for_role(company, role)

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
    """ترحيل فاتورة المورد وتسوية حساب الاستلامات وضريبة المدخلات والدائنين."""
    invoice = PurchaseInvoice.objects.select_for_update().get(pk=invoice_id)
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
    lines = list(invoice.lines.select_related("receipt_line", "receipt_line__product"))
    expected = list(receipt.stock_movement.lines.all())
    if len(lines) != len(expected) or {x.receipt_line_id for x in lines} != {x.pk for x in expected}:
        raise ValidationError("أصناف الفاتورة لا تطابق إذن الاستلام.")
    grni_amount = sum((line.cost_amount for line in expected), Decimal("0")).quantize(Decimal("0.0001"))
    net_amount = sum((line.net_amount for line in lines), Decimal("0")).quantize(Decimal("0.0001"))
    tax_amount = sum((line.tax_amount for line in lines), Decimal("0")).quantize(Decimal("0.0001"))
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

    payable = _account(invoice.company, AccountRole.Role.ACCOUNTS_PAYABLE)
    grni = _account(invoice.company, AccountRole.Role.GOODS_RECEIVED_NOT_INVOICED)
    vat_input = _account(invoice.company, AccountRole.Role.VAT_INPUT) if tax_amount > 0 else None
    if len({account.pk for account in (payable, grni, *([vat_input] if vat_input else []))}) != (3 if vat_input else 2):
        raise ValidationError("يجب ربط حسابات الموردين والبضاعة غير المفوترة وضريبة المدخلات بحسابات مختلفة.")

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
    if vat_input and tax_amount:
        journal_lines.append(JournalLine(entry=entry, account=vat_input, description="ضريبة قيمة مضافة مدخلات", debit=tax_amount, credit=0))
    JournalLine.objects.bulk_create(journal_lines)
    try:
        posted_entry = post_journal(entry.pk)
    except DjangoValidationError as exc:
        detail = exc.message_dict if hasattr(exc, "message_dict") else exc.messages
        raise ValidationError(detail) from exc

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
