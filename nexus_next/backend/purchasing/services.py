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

from .models import PurchaseInvoice, PurchaseReceipt


def _account(company, role):
    assignment = AccountRole.objects.select_related("account").filter(company=company, role=role).first()
    if not assignment:
        raise ValidationError(f"يجب ربط دور الحساب ({role}) في إعدادات الشركة.")
    account = assignment.account
    if account.company_id != company.pk or not account.is_active or account.is_group or not account.allow_posting:
        raise ValidationError(f"الحساب المرتبط بالدور ({role}) غير صالح للترحيل.")
    return account


def _branch_and_year(receipt, entry_date):
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
    receipt = PurchaseReceipt.objects.select_for_update().select_related(
        "company", "supplier", "warehouse", "stock_movement"
    ).get(pk=receipt_id)
    if receipt.stock_movement.status != StockMovement.Status.DRAFT:
        raise ValidationError("إذن الاستلام مرحّل بالفعل.")
    if receipt.supplier.company_id != receipt.company_id or receipt.warehouse.company_id != receipt.company_id:
        raise ValidationError("بيانات المورد أو المخزن لا تتبع الشركة.")
    post_stock_movement(receipt.stock_movement_id, user)
    return PurchaseReceipt.objects.select_related("stock_movement").get(pk=receipt.pk)


@transaction.atomic
def post_purchase_invoice(invoice_id, user):
    invoice = PurchaseInvoice.objects.select_for_update().get(pk=invoice_id)
    if invoice.status != PurchaseInvoice.Status.DRAFT:
        raise ValidationError("فاتورة المورد مرحلة بالفعل.")
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
    from .models import SupplierPayment

    payment = SupplierPayment.objects.select_for_update().get(pk=payment_id)
    if payment.status != SupplierPayment.Status.DRAFT:
        raise ValidationError("سند صرف المورد مرحّل بالفعل.")
    invoice = PurchaseInvoice.objects.select_for_update().get(pk=payment.invoice_id)
    if invoice.status != PurchaseInvoice.Status.POSTED:
        raise ValidationError("لا يمكن صرف قيمة فاتورة غير مرحلة.")
    if payment.company_id != invoice.company_id or payment.supplier_id != invoice.supplier_id:
        raise ValidationError("بيانات السند لا تطابق فاتورة المورد.")

    paid_before = SupplierPayment.objects.filter(
        invoice=invoice, status=SupplierPayment.Status.POSTED
    ).exclude(pk=payment.pk).aggregate(total=Sum("amount"))["total"] or Decimal("0")
    if paid_before + payment.amount > invoice.grand_total:
        remaining = max(Decimal("0"), invoice.grand_total - paid_before)
        raise ValidationError(f"قيمة السند تتجاوز المتبقي على الفاتورة ({remaining}).")

    payable = _account(payment.company, AccountRole.Role.ACCOUNTS_PAYABLE)
    cash_role = AccountRole.Role.CASH if payment.method == SupplierPayment.Method.CASH else AccountRole.Role.BANK
    payment_account = _account(payment.company, cash_role)
    if payable.pk == payment_account.pk:
        raise ValidationError("حساب الموردين وحساب الصندوق/البنك يجب أن يكونا مختلفين.")

    receipt = invoice.receipt
    branch, year = _branch_and_year(receipt, payment.payment_date)
    entry = JournalEntry.objects.create(
        company=payment.company, branch=branch, fiscal_year=year,
        entry_number=f"PUR-PAY-{payment.pk}", entry_date=payment.payment_date,
        description=f"سداد فاتورة المورد {invoice.invoice_number} - سند {payment.payment_number}"[:500],
        source_type=JournalEntry.Source.PURCHASES, created_by=user,
    )
    JournalLine.objects.bulk_create([
        JournalLine(entry=entry, account=payable, description="تخفيض مستحقات المورد", debit=payment.amount, credit=0),
        JournalLine(entry=entry, account=payment_account, description="صرف نقدي/بنكي للمورد", debit=0, credit=payment.amount),
    ])
    try:
        posted_entry = post_journal(entry.pk)
    except DjangoValidationError as exc:
        detail = exc.message_dict if hasattr(exc, "message_dict") else exc.messages
        raise ValidationError(detail) from exc

    payment.journal_entry = posted_entry
    payment.status = SupplierPayment.Status.POSTED
    payment.posted_at = timezone.now()
    payment.save(update_fields=("journal_entry", "status", "posted_at"))
    return payment
