"""ترحيل مردودات المبيعات مع عكس الإيراد والضريبة والتكلفة دون تعديل الفاتورة الأصلية."""

from decimal import Decimal

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from accounting.models import AccountRole, JournalEntry, JournalLine
from accounting.services import reverse_operational_journal
from accounting.services import get_account_for_role, post_journal
from inventory.models import Product, StockMovement, StockMovementLine
from inventory.services import post_stock_movement
from audit.models import AuditEvent
from .models import SalesInvoice, SalesReturn
from .services import _branch_and_year


def _return_account(company, role):
    return get_account_for_role(company, role)


def _post_entry(entry):
    try:
        return post_journal(entry.pk)
    except DjangoValidationError as exc:
        detail = exc.message_dict if hasattr(exc, "message_dict") else exc.messages
        raise ValidationError(detail) from exc


@transaction.atomic
def post_sales_return(return_id, user):
    sales_return = SalesReturn.objects.select_for_update().select_related(
        "company", "invoice", "warehouse", "stock_movement"
    ).get(pk=return_id)
    if sales_return.status != SalesReturn.Status.DRAFT:
        raise ValidationError("مردود المبيعات مرحّل بالفعل.")

    invoice = SalesInvoice.objects.select_for_update().get(pk=sales_return.invoice_id)
    if invoice.status != SalesInvoice.Status.POSTED:
        raise ValidationError("لا يمكن ترحيل مردود لفاتورة غير مرحلة.")
    if sales_return.stock_movement.status != StockMovement.Status.DRAFT:
        raise ValidationError("حركة استلام المردود ليست مسودة.")
    lines = list(sales_return.lines.select_related(
        "invoice_line", "invoice_line__delivery_line", "movement_line"
    ).order_by("invoice_line_id"))
    if not lines:
        raise ValidationError("لا يمكن ترحيل مردود بدون أصناف.")

    # Lock the original invoice lines so concurrent return postings cannot exceed sold quantities.
    original_ids = [line.invoice_line_id for line in lines]
    originals = {
        row.pk: row for row in invoice.lines.select_for_update().filter(pk__in=original_ids).order_by("pk")
    }
    if len(originals) != len(set(original_ids)):
        raise ValidationError("بعض بنود المردود لا تتبع الفاتورة الأصلية.")
    for line in lines:
        original = originals[line.invoice_line_id]
        already_returned = SalesReturn.objects.filter(
            invoice_id=invoice.pk, status=SalesReturn.Status.POSTED,
            lines__invoice_line_id=original.pk,
        ).exclude(pk=sales_return.pk).aggregate(total=Sum("lines__quantity"))["total"] or Decimal("0")
        if already_returned + line.quantity > original.delivery_line.quantity:
            remaining = max(original.delivery_line.quantity - already_returned, Decimal("0"))
            raise ValidationError(
                f"كمية المرتجع للصنف {original.delivery_line.product.code} تتجاوز الكمية المباعة؛ المتبقي {remaining}."
            )
        if line.movement_line.product_id != original.delivery_line.product_id:
            raise ValidationError("صنف حركة الاستلام لا يطابق بند الفاتورة الأصلية.")

    net = sum((line.net_amount for line in lines), Decimal("0")).quantize(Decimal("0.0001"))
    tax = sum((line.tax_amount for line in lines), Decimal("0")).quantize(Decimal("0.0001"))
    total = net + tax
    cost = sum((line.cost_amount for line in lines), Decimal("0")).quantize(Decimal("0.0001"))

    delivery = invoice.delivery
    branch, year = _branch_and_year(delivery, sales_return.return_date)

    # Restock at the original recorded delivery cost; the standard receipt posting first
    # credits GRNI, then the clearing entry below reclassifies that credit to COGS.
    post_stock_movement(sales_return.stock_movement_id, user)
    grni = _return_account(sales_return.company, AccountRole.Role.GOODS_RECEIVED_NOT_INVOICED)
    cogs = _return_account(sales_return.company, AccountRole.Role.COGS)
    cost_entry = None
    if cost > 0:
        if grni.pk == cogs.pk:
            raise ValidationError("حساب البضاعة المستلمة غير المفوترة يجب أن يختلف عن حساب تكلفة المبيعات.")
        cost_entry = JournalEntry.objects.create(
            company=sales_return.company, branch=branch, fiscal_year=year,
            entry_number=f"SALE-RET-COST-{sales_return.pk}", entry_date=sales_return.return_date,
            description=f"عكس تكلفة مردود المبيعات {sales_return.return_number}"[:500],
            source_type=JournalEntry.Source.SALES, created_by=user,
        )
        JournalLine.objects.bulk_create([
            JournalLine(entry=cost_entry, account=grni, description="تسوية استلام مردود المبيعات", debit=cost, credit=0),
            JournalLine(entry=cost_entry, account=cogs, description="عكس تكلفة البضاعة المرتجعة", debit=0, credit=cost),
        ])
        cost_entry = _post_entry(cost_entry)

    receivable = _return_account(sales_return.company, AccountRole.Role.ACCOUNTS_RECEIVABLE)
    sales_returns = _return_account(sales_return.company, AccountRole.Role.SALES_RETURNS) if net else None
    vat_output = _return_account(sales_return.company, AccountRole.Role.VAT_OUTPUT) if tax else None
    accounts = [receivable] + ([sales_returns] if sales_returns else []) + ([vat_output] if vat_output else [])
    if len({account.pk for account in accounts}) != len(accounts):
        raise ValidationError("حساب مردودات المبيعات والضريبة والعملاء يجب أن تكون مختلفة.")
    journal = JournalEntry.objects.create(
        company=sales_return.company, branch=branch, fiscal_year=year,
        entry_number=f"SALE-RET-{sales_return.pk}", entry_date=sales_return.return_date,
        description=f"إشعار مردود مبيعات للفاتورة {invoice.invoice_number} رقم {sales_return.return_number}"[:500],
        source_type=JournalEntry.Source.SALES, created_by=user,
    )
    journal_lines = []
    if sales_returns and net:
        journal_lines.append(JournalLine(
            entry=journal, account=sales_returns, description="مردودات المبيعات", debit=net, credit=0
        ))
    if vat_output and tax:
        journal_lines.append(JournalLine(
            entry=journal, account=vat_output, description="عكس ضريبة المخرجات", debit=tax, credit=0
        ))
    journal_lines.append(JournalLine(
        entry=journal, account=receivable, description="تخفيض مديونية العميل", debit=0, credit=total
    ))
    JournalLine.objects.bulk_create(journal_lines)
    posted = _post_entry(journal)

    sales_return.net_total = net
    sales_return.tax_total = tax
    sales_return.grand_total = total
    sales_return.journal_entry = posted
    sales_return.cost_journal_entry = cost_entry
    sales_return.status = SalesReturn.Status.POSTED
    sales_return.posted_at = timezone.now()
    sales_return.save(update_fields=(
        "net_total", "tax_total", "grand_total", "journal_entry",
        "cost_journal_entry", "status", "posted_at",
    ))
    return sales_return


@transaction.atomic
def reverse_sales_return(return_id, user, entry_date):
    """عكس مردود المبيعات مع استهلاك طبقات تكلفة الاستلام الأصلي فقط."""
    document = SalesReturn.objects.select_for_update().select_related(
        "company", "invoice", "warehouse", "journal_entry", "stock_movement",
    ).get(pk=return_id)
    if document.status != SalesReturn.Status.POSTED or not document.journal_entry_id:
        raise ValidationError("لا يمكن عكس إلا مردود مبيعات مرحّل.")
    if document.reversal_journal_entry_id or document.reversal_stock_movement_id:
        raise ValidationError("تم عكس مردود المبيعات من قبل.")
    if entry_date < document.return_date:
        raise ValidationError("تاريخ العكس لا يجوز أن يسبق تاريخ المردود.")
    if document.journal_entry.status != JournalEntry.Status.POSTED:
        raise ValidationError("قيد مردود المبيعات الأصلي ليس مرحّلًا.")

    lines = list(document.lines.select_for_update(of=("self",)).select_related(
        "movement_line__destination_location", "invoice_line__delivery_line__product",
    ).order_by("id"))
    if not lines:
        raise ValidationError("لا توجد بنود مخزون مرتبطة بمردود المبيعات.")

    reversal_movement = StockMovement.objects.create(
        company=document.company,
        movement_number=f"SRETREV-{document.pk}",
        movement_type=StockMovement.MovementType.SALES_RETURN_REVERSAL,
        movement_date=entry_date,
        source_warehouse=document.warehouse,
        notes=f"عكس مردود المبيعات {document.return_number}",
        created_by=user,
    )
    for line in lines:
        product = line.invoice_line.delivery_line.product
        if product.costing_method not in (
            Product.CostingMethod.FIFO, Product.CostingMethod.WEIGHTED_AVERAGE,
        ):
            raise ValidationError("عكس مردود المبيعات يتطلب استمرار تتبع تكلفة FIFO أو المتوسط المرجح.")
        reversal_line = StockMovementLine.objects.create(
            movement=reversal_movement,
            product=product,
            quantity=line.quantity,
            source_location=line.movement_line.destination_location,
            expiry_date=line.movement_line.expiry_date,
            reversal_of_line=line.movement_line,
        )
        if reversal_line.product_id != line.movement_line.product_id:
            raise ValidationError("بند عكس المخزون لا يطابق بند الاستلام الأصلي.")

    posted_movement = post_stock_movement(reversal_movement.pk, user)
    for line in lines:
        reversal_line = posted_movement.lines.get(reversal_of_line=line.movement_line)
        if abs(reversal_line.cost_amount - line.cost_amount) > Decimal("0.0001"):
            raise ValidationError("تكلفة المخزون المتبقية من مردود المبيعات لا تطابق التكلفة الأصلية؛ تم إلغاء العكس.")

    try:
        reversal_entry = reverse_operational_journal(
            document.journal_entry_id, user, entry_date, JournalEntry.Source.SALES,
        )
    except DjangoValidationError as exc:
        detail = exc.message_dict if hasattr(exc, "message_dict") else exc.messages
        raise ValidationError(detail) from exc

    document.reversal_stock_movement = posted_movement
    document.reversal_journal_entry = reversal_entry
    document.reversal_date = entry_date
    document.reversed_at = timezone.now()
    document.reversed_by = user
    document.status = SalesReturn.Status.REVERSED
    document.save(update_fields=(
        "reversal_stock_movement", "reversal_journal_entry", "reversal_date",
        "reversed_at", "reversed_by", "status",
    ))
    AuditEvent.objects.create(
        actor=user, company=document.company, branch=document.warehouse.branch,
        action=AuditEvent.Action.ACTION, resource="sales.sales_return.reversed",
        object_id=str(document.pk), http_method="POST", status_code=200, succeeded=True,
        metadata={
            "return_number": document.return_number,
            "reversal_date": entry_date.isoformat(),
            "reversal_stock_movement_id": posted_movement.pk,
            "reversal_journal_entry_id": reversal_entry.pk,
        },
    )
    return document
