from decimal import Decimal

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from rest_framework.exceptions import ValidationError

from accounting.models import AccountRole, JournalEntry, JournalLine
from accounting.services import get_account_for_role
from accounting.services import post_journal
from companies.models import Branch, FiscalYear

from .models import StockMovement, StockMovementJournalLink


def _role_account(company, role):
    """استخراج الحساب التفصيلي المرتبط بدور المخزون المطلوب."""
    return get_account_for_role(company, role)

def _branch_for(warehouse, company):
    """تحديد الفرع المحاسبي المرتبط بالمخزن وحركة المخزون."""
    if warehouse.branch_id:
        if not warehouse.branch.is_active:
            raise ValidationError("فرع المخزن موقوف ولا يمكن الترحيل عليه.")
        return warehouse.branch
    branch = Branch.objects.filter(company=company, is_active=True, is_head_office=True).first()
    if branch:
        return branch
    branch = Branch.objects.filter(company=company, is_active=True).order_by("code").first()
    if not branch:
        raise ValidationError("يجب إنشاء فرع نشط للشركة قبل ترحيل قيد المخزون.")
    return branch


def _fiscal_year(company, movement):
    """تحديد السنة المالية الصالحة لتاريخ حركة المخزون."""
    year = FiscalYear.objects.filter(
        company=company, status=FiscalYear.Status.OPEN,
        start_date__lte=movement.movement_date, end_date__gte=movement.movement_date,
    ).order_by("-start_date").first()
    if not year:
        raise ValidationError("لا توجد سنة مالية مفتوحة تشمل تاريخ حركة المخزون.")
    return year


@transaction.atomic
def _create_posted_entry(movement, user, fiscal_year, branch, suffix, debit_account, credit_account, amount, description):
    """إنشاء قيد المخزون المتوازن وربطه بحركة المخزون."""
    amount = amount.quantize(Decimal("0.0001"))
    if amount <= 0:
        return None
    if debit_account.pk == credit_account.pk:
        raise ValidationError("لا يمكن استخدام نفس الحساب في طرفي قيد المخزون.")
    entry = JournalEntry.objects.create(
        company=movement.company, branch=branch, fiscal_year=fiscal_year,
        entry_number=f"INV-{movement.pk}-{suffix}", entry_date=movement.movement_date,
        description=description[:500], source_type=JournalEntry.Source.INVENTORY, created_by=user,
    )
    JournalLine.objects.bulk_create([
        JournalLine(entry=entry, account=debit_account, description=description[:300], debit=amount, credit=0),
        JournalLine(entry=entry, account=credit_account, description=description[:300], debit=0, credit=amount),
    ])
    try:
        posted = post_journal(entry.pk)
    except DjangoValidationError as exc:
        detail = exc.message_dict if hasattr(exc, "message_dict") else exc.messages
        raise ValidationError(detail) from exc
    StockMovementJournalLink.objects.create(movement=movement, journal_entry=posted)
    return posted


def post_movement_journals(movement, user):
    """إنشاء القيود المحاسبية اللازمة لحركة المخزون دون تكرار الترحيل."""
    if movement.movement_type == StockMovement.MovementType.SUPPLIER_RETURN:
        return []
    lines = list(movement.lines.select_related("product"))
    amounts = [line.cost_amount for line in lines]
    total = sum(amounts, Decimal("0"))
    if total <= 0:
        return []
    year = _fiscal_year(movement.company, movement)
    inventory = _role_account(movement.company, AccountRole.Role.INVENTORY)

    if movement.movement_type == StockMovement.MovementType.RECEIPT:
        grni = _role_account(movement.company, AccountRole.Role.GOODS_RECEIVED_NOT_INVOICED)
        branch = _branch_for(movement.destination_warehouse, movement.company)
        entry = _create_posted_entry(movement, user, year, branch, "R", inventory, grni, total, f"استلام مخزني {movement.movement_number}")
        return [entry] if entry else []

    if movement.movement_type == StockMovement.MovementType.ISSUE:
        cogs = _role_account(movement.company, AccountRole.Role.COGS)
        branch = _branch_for(movement.source_warehouse, movement.company)
        entry = _create_posted_entry(movement, user, year, branch, "I", cogs, inventory, total, f"صرف مخزني {movement.movement_number}")
        return [entry] if entry else []

    if movement.movement_type == StockMovement.MovementType.ADJUSTMENT:
        direction = lines[0].adjustment_direction
        if any(line.adjustment_direction != direction for line in lines):
            raise ValidationError("افصل زيادات الجرد عن العجز في حركتين مستقلتين.")
        branch = _branch_for(
            movement.destination_warehouse if direction == "in" else movement.source_warehouse,
            movement.company,
        )
        if direction == "in":
            gain = _role_account(movement.company, AccountRole.Role.INVENTORY_ADJUSTMENT_GAIN)
            entry = _create_posted_entry(movement, user, year, branch, "A", inventory, gain, total, f"زيادة جرد {movement.movement_number}")
        else:
            loss = _role_account(movement.company, AccountRole.Role.INVENTORY_ADJUSTMENT_LOSS)
            entry = _create_posted_entry(movement, user, year, branch, "A", loss, inventory, total, f"عجز جرد {movement.movement_number}")
        return [entry] if entry else []

    if movement.movement_type == StockMovement.MovementType.TRANSFER:
        source_branch = _branch_for(movement.source_warehouse, movement.company)
        destination_branch = _branch_for(movement.destination_warehouse, movement.company)
        if source_branch.pk == destination_branch.pk:
            return []
        clearing = _role_account(movement.company, AccountRole.Role.INTER_BRANCH_CLEARING)
        source_entry = _create_posted_entry(
            movement, user, year, source_branch, "S", clearing, inventory, total,
            f"تحويل صادر بين الفروع {movement.movement_number}",
        )
        destination_entry = _create_posted_entry(
            movement, user, year, destination_branch, "D", inventory, clearing, total,
            f"تحويل وارد بين الفروع {movement.movement_number}",
        )
        return [entry for entry in (source_entry, destination_entry) if entry]

    return []
