from decimal import Decimal

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from accounting.models import AccountRole, JournalEntry, JournalLine
from accounting.services import post_journal
from companies.models import Branch, FiscalYear
from inventory.models import StockMovement
from inventory.services import post_stock_movement
from approvals.snapshots import invoice_discount_snapshot

from .models import CustomerReceipt, SalesDelivery, SalesInvoice


def _account(company, role):
    assignment = AccountRole.objects.select_related("account").filter(company=company, role=role).first()
    if not assignment:
        raise ValidationError(f"يجب ربط دور الحساب ({role}) في إعدادات الشركة.")
    account = assignment.account
    if account.company_id != company.pk or not account.is_active or account.is_group or not account.allow_posting:
        raise ValidationError(f"الحساب المرتبط بالدور ({role}) غير صالح للترحيل.")
    return account


def _branch_and_year(delivery, entry_date):
    warehouse = delivery.warehouse
    branch = warehouse.branch if warehouse.branch_id else None
    if branch and not branch.is_active:
        raise ValidationError("فرع المخزن موقوف.")
    if not branch:
        branch = Branch.objects.filter(company=delivery.company, is_active=True, is_head_office=True).first()
        if not branch:
            branch = Branch.objects.filter(company=delivery.company, is_active=True).order_by("code").first()
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
    receipt = CustomerReceipt.objects.select_for_update().get(pk=receipt_id)
    if receipt.status != CustomerReceipt.Status.DRAFT:
        raise ValidationError("سند القبض مرحّل بالفعل.")
    invoice = SalesInvoice.objects.select_for_update().get(pk=receipt.invoice_id)
    if invoice.status != SalesInvoice.Status.POSTED:
        raise ValidationError("لا يمكن قبض فاتورة غير مرحلة.")
    if receipt.company_id != invoice.company_id or receipt.customer_id != invoice.customer_id:
        raise ValidationError("بيانات السند لا تطابق فاتورة العميل.")
    paid = CustomerReceipt.objects.filter(
        invoice=invoice, status=CustomerReceipt.Status.POSTED
    ).exclude(pk=receipt.pk).aggregate(total=Sum("amount"))["total"] or Decimal("0")
    if paid + receipt.amount > invoice.grand_total:
        remaining = max(Decimal("0"), invoice.grand_total - paid)
        raise ValidationError(f"قيمة السند تتجاوز المتبقي على الفاتورة ({remaining}).")
    receivable = _account(receipt.company, AccountRole.Role.ACCOUNTS_RECEIVABLE)
    cash_role = AccountRole.Role.CASH if receipt.method == CustomerReceipt.Method.CASH else AccountRole.Role.BANK
    cash = _account(receipt.company, cash_role)
    if receivable.pk == cash.pk:
        raise ValidationError("حساب العملاء وحساب الصندوق/البنك يجب أن يكونا مختلفين.")
    branch, year = _branch_and_year(invoice.delivery, receipt.receipt_date)
    entry = JournalEntry.objects.create(
        company=receipt.company, branch=branch, fiscal_year=year,
        entry_number=f"SALE-REC-{receipt.pk}", entry_date=receipt.receipt_date,
        description=f"سند قبض العميل {invoice.customer.name_ar} رقم {receipt.receipt_number}"[:500],
        source_type=JournalEntry.Source.SALES, created_by=user,
    )
    JournalLine.objects.bulk_create([
        JournalLine(entry=entry, account=cash, description="استلام نقدية/تحويل من العميل", debit=receipt.amount, credit=0),
        JournalLine(entry=entry, account=receivable, description="تخفيض مستحقات العميل", debit=0, credit=receipt.amount),
    ])
    try:
        posted = post_journal(entry.pk)
    except DjangoValidationError as exc:
        detail = exc.message_dict if hasattr(exc, "message_dict") else exc.messages
        raise ValidationError(detail) from exc
    receipt.journal_entry = posted
    receipt.status = CustomerReceipt.Status.POSTED
    receipt.posted_at = timezone.now()
    receipt.save(update_fields=("journal_entry", "status", "posted_at"))
    return receipt
