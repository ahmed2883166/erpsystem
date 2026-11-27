from decimal import Decimal

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from companies.models import Company

from .models import (
    StockMovement, StockMovementLine, StockTransfer, StockTransferLine,
    StockTransferReceipt, StockTransferReceiptLine, Warehouse,
)
from .services import post_stock_movement


@transaction.atomic
def _transit_warehouse(company_id):
    """إنشاء مخزن وسيط وحيد للشركة مع قفل الشركة لمنع التكرار المتزامن."""
    company = Company.objects.select_for_update().get(pk=company_id)
    warehouse = Warehouse.objects.filter(company=company, is_transit=True).first()
    if warehouse:
        if not warehouse.is_active:
            raise ValidationError("مخزن البضاعة بالطريق موقوف؛ فعّله قبل تنفيذ التحويل.")
        return warehouse
    base_code = f"TRANSIT-{company.pk}"
    code = base_code
    suffix = 1
    while Warehouse.objects.filter(company=company, code=code).exists():
        code = f"{base_code}-{suffix}"
        suffix += 1
    return Warehouse.objects.create(
        company=company,
        branch=None,
        code=code[:40],
        name_ar="مخزن البضاعة بالطريق",
        address="مخزن نظامي وسيط للتحويلات بين الفروع",
        is_active=True,
        is_transit=True,
    )


@transaction.atomic
def dispatch_stock_transfer(transfer_id, user):
    """شحن التحويل إلى مخزن البضاعة بالطريق مع ترحيل الكمية والتكلفة وقيود الفروع."""
    transfer = (
        StockTransfer.objects.select_for_update()
        .select_related("company", "source_warehouse", "destination_warehouse")
        .get(pk=transfer_id)
    )
    if transfer.status != StockTransfer.Status.DRAFT:
        raise ValidationError("لا يمكن شحن التحويل إلا مرة واحدة وهو في حالة المسودة.")
    lines = list(
        StockTransferLine.objects.select_for_update()
        .select_related("product", "product__unit")
        .filter(transfer=transfer).order_by("product_id", "id")
    )
    if not lines:
        raise ValidationError("لا يمكن شحن تحويل بدون أصناف.")
    if not transfer.source_warehouse.is_active or not transfer.destination_warehouse.is_active:
        raise ValidationError("مخزن المصدر أو الوجهة موقوف.")
    transit = _transit_warehouse(transfer.company_id)
    movement = StockMovement.objects.create(
        company=transfer.company,
        movement_number=f"TR-{transfer.pk}-OUT",
        movement_type=StockMovement.MovementType.TRANSFER,
        movement_date=transfer.transfer_date,
        source_warehouse=transfer.source_warehouse,
        destination_warehouse=transit,
        notes=f"شحن تحويل بين الفروع {transfer.transfer_number}",
        created_by=user,
    )
    StockMovementLine.objects.bulk_create([
        StockMovementLine(
            movement=movement,
            product=line.product,
            quantity=line.quantity,
            unit_cost=Decimal("0"),
            expiry_date=line.expiry_date,
        )
        for line in lines
    ])
    movement = post_stock_movement(movement.pk, user)
    transfer.dispatch_movement = movement
    transfer.status = StockTransfer.Status.IN_TRANSIT
    transfer.dispatched_by = user
    transfer.dispatched_at = timezone.now()
    transfer.save(update_fields=(
        "dispatch_movement", "status", "dispatched_by", "dispatched_at", "updated_at",
    ))
    return transfer


@transaction.atomic
def receive_stock_transfer(transfer_id, user, *, receipt_date, lines):
    """تسجيل استلام جزئي متعدد المرات دون إظهار غير المستلم في مخزن الوجهة."""
    transfer = (
        StockTransfer.objects.select_for_update()
        .select_related("company", "source_warehouse", "destination_warehouse")
        .get(pk=transfer_id)
    )
    if transfer.status not in (
        StockTransfer.Status.IN_TRANSIT,
        StockTransfer.Status.PARTIALLY_RECEIVED,
    ):
        raise ValidationError("لا يمكن الاستلام إلا لتحويل تم شحنه وما زال به رصيد بالطريق.")
    if receipt_date < transfer.transfer_date:
        raise ValidationError("تاريخ الاستلام لا يمكن أن يسبق تاريخ الشحن.")
    transit = _transit_warehouse(transfer.company_id)
    transfer_lines = {
        line.pk: line
        for line in StockTransferLine.objects.select_for_update()
        .select_related("product", "product__unit")
        .filter(transfer=transfer).order_by("pk")
    }
    requested_ids = [item["transfer_line"] for item in lines]
    if len(requested_ids) != len(set(requested_ids)):
        raise ValidationError("لا تكرر بند التحويل في مستند الاستلام الواحد.")
    selected = []
    for item in lines:
        transfer_line = transfer_lines.get(item["transfer_line"])
        if transfer_line is None:
            raise ValidationError("يوجد بند استلام لا يتبع مستند التحويل المحدد.")
        quantity = item["quantity"]
        if not isinstance(quantity, Decimal):
            quantity = Decimal(str(quantity))
        if quantity <= 0:
            raise ValidationError("كمية الاستلام يجب أن تكون أكبر من صفر.")
        if quantity > transfer_line.remaining_quantity:
            raise ValidationError(
                f"كمية استلام الصنف {transfer_line.product.code} تتجاوز الكمية المتبقية بالطريق."
            )
        quantum = Decimal("1").scaleb(-transfer_line.product.unit.decimal_places)
        if quantity != quantity.quantize(quantum):
            raise ValidationError(
                f"كمية استلام الصنف {transfer_line.product.code} تتجاوز دقة وحدة القياس."
            )
        selected.append((transfer_line, quantity))

    if not selected:
        raise ValidationError("أدخل بند استلام واحدًا على الأقل.")
    sequence = transfer.receipts.count() + 1
    receipt_number = f"TR-{transfer.pk}-R{sequence}"
    movement = StockMovement.objects.create(
        company=transfer.company,
        movement_number=f"TR-{transfer.pk}-IN-{sequence}",
        movement_type=StockMovement.MovementType.TRANSFER,
        movement_date=receipt_date,
        source_warehouse=transit,
        destination_warehouse=transfer.destination_warehouse,
        notes=f"استلام تحويل بين الفروع {transfer.transfer_number} - {receipt_number}",
        created_by=user,
    )
    movement_lines = []
    for transfer_line, quantity in selected:
        movement_lines.append(StockMovementLine(
            movement=movement,
            product=transfer_line.product,
            quantity=quantity,
            unit_cost=Decimal("0"),
            expiry_date=transfer_line.expiry_date,
        ))
    movement_lines = StockMovementLine.objects.bulk_create(movement_lines)
    movement = post_stock_movement(movement.pk, user)

    receipt = StockTransferReceipt.objects.create(
        transfer=transfer,
        receipt_number=receipt_number,
        receipt_date=receipt_date,
        movement=movement,
        received_by=user,
    )
    for (transfer_line, quantity), movement_line in zip(selected, movement_lines):
        StockTransferReceiptLine.objects.create(
            receipt=receipt,
            transfer_line=transfer_line,
            movement_line=movement_line,
            quantity=quantity,
        )
        transfer_line.received_quantity += quantity
        transfer_line.save(update_fields=("received_quantity",))

    all_received = all(
        line.received_quantity == line.quantity
        for line in transfer_lines.values()
    )
    transfer.status = (
        StockTransfer.Status.RECEIVED if all_received
        else StockTransfer.Status.PARTIALLY_RECEIVED
    )
    if all_received:
        transfer.completed_at = timezone.now()
    transfer.save(update_fields=("status", "completed_at", "updated_at"))
    return receipt
