"""خدمات الجرد الفعلي وربط فروقه بحركات المخزون المرحلة."""

from decimal import Decimal

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .models import (
    InventoryCount, InventoryCountLine, Product, StockBalance, StockMovement,
    StockMovementLine, Warehouse,
)


@transaction.atomic
def post_inventory_count(count_id, user):
    """اعتماد الجرد بعد التحقق من عدم تغير المخزون منذ أخذ اللقطة."""
    count = InventoryCount.objects.select_for_update().select_related(
        "company", "warehouse",
    ).get(pk=count_id)
    if count.status != InventoryCount.Status.DRAFT:
        raise ValidationError("مستند الجرد مرحّل بالفعل أو ملغي.")

    lines = list(
        count.lines.select_for_update().select_related(
            "product__unit", "location",
        ).order_by("product_id", "id")
    )
    if not lines:
        raise ValidationError("لا يمكن اعتماد جرد بدون بنود.")
    if any(line.counted_quantity is None for line in lines):
        raise ValidationError("أكمل العد الفعلي لكل البنود قبل اعتماد الجرد.")

    product_ids = sorted({line.product_id for line in lines})
    products = list(
        Product.objects.select_for_update().select_related("unit")
        .filter(pk__in=product_ids).order_by("pk")
    )
    products_by_id = {product.pk: product for product in products}
    if len(products_by_id) != len(product_ids):
        raise ValidationError("يوجد صنف غير موجود ضمن مستند الجرد.")

    warehouse = Warehouse.objects.select_for_update().get(pk=count.warehouse_id)
    if not warehouse.is_active:
        raise ValidationError("لا يمكن اعتماد جرد مخزن موقوف.")

    # The snapshot becomes stale if any movement was created or posted after it.
    later_activity = StockMovement.objects.filter(
        Q(source_warehouse_id=warehouse.pk) | Q(destination_warehouse_id=warehouse.pk),
    ).filter(
        Q(created_at__gt=count.created_at) | Q(posted_at__gt=count.created_at),
    )
    if later_activity.exists():
        raise ValidationError(
            "حدثت حركة مخزنية بعد بدء الجرد. ألغِ هذا الجرد وابدأ جردًا جديدًا لضمان صحة الفروق."
        )

    # Verify the stored snapshot against the live balances as a second safeguard.
    snapshot = {
        (line.product_id, line.location_id, line.expiry_date): line
        for line in lines
    }
    current_balances = list(
        StockBalance.objects.select_for_update().filter(
            warehouse=warehouse, quantity__gt=0,
        ).order_by("product_id", "location_id", "expiry_date", "id")
    )
    current_keys = set()
    for balance in current_balances:
        key = (balance.product_id, balance.location_id, balance.expiry_date)
        current_keys.add(key)
        line = snapshot.get(key)
        if line is None or line.system_quantity != balance.quantity or line.system_value != balance.inventory_value:
            raise ValidationError(
                "تغير رصيد المخزون منذ بدء الجرد. ألغِ هذا الجرد وابدأ جردًا جديدًا."
            )
    if current_keys != {key for key, line in snapshot.items() if line.system_quantity > 0}:
        raise ValidationError("أبعاد رصيد المخزون لا تطابق لقطة الجرد؛ أعد بدء الجرد.")

    gains = []
    losses = []
    for line in lines:
        product = products_by_id[line.product_id]
        if not product.is_active or not product.unit.is_active:
            raise ValidationError(f"الصنف أو وحدة قياس {product.code} موقوف.")
        quantum = Decimal("1").scaleb(-product.unit.decimal_places)
        if line.counted_quantity != line.counted_quantity.quantize(quantum):
            raise ValidationError(f"كمية الجرد للصنف {product.code} تتجاوز دقة وحدة القياس.")
        variance = line.counted_quantity - line.system_quantity
        if variance > 0:
            if line.adjustment_unit_cost <= 0:
                raise ValidationError(
                    f"أدخل تكلفة معتمدة أكبر من صفر للزيادة في الصنف {product.code} قبل الترحيل."
                )
            gains.append((line, variance))
        elif variance < 0:
            losses.append((line, -variance))

    from .services import post_stock_movement

    def create_adjustment(suffix, direction, rows):
        if not rows:
            return None
        movement_number = f"IC{count.pk}-{suffix}"
        movement = StockMovement.objects.create(
            company=count.company,
            movement_number=movement_number,
            movement_type=StockMovement.MovementType.ADJUSTMENT,
            movement_date=count.count_date,
            source_warehouse=warehouse if direction == StockMovementLine.AdjustmentDirection.OUT else None,
            destination_warehouse=warehouse if direction == StockMovementLine.AdjustmentDirection.IN else None,
            notes=f"تسوية فروق الجرد {count.count_number}",
            created_by=user,
        )
        StockMovementLine.objects.bulk_create([
            StockMovementLine(
                movement=movement,
                product=products_by_id[line.product_id],
                source_location=line.location if direction == StockMovementLine.AdjustmentDirection.OUT else None,
                destination_location=line.location if direction == StockMovementLine.AdjustmentDirection.IN else None,
                expiry_date=line.expiry_date,
                quantity=variance,
                unit_cost=line.adjustment_unit_cost,
                adjustment_direction=direction,
            )
            for line, variance in rows
        ])
        return post_stock_movement(movement.pk, user, inventory_count_id=count.pk)

    gain_movement = create_adjustment("G", StockMovementLine.AdjustmentDirection.IN, gains)
    loss_movement = create_adjustment("L", StockMovementLine.AdjustmentDirection.OUT, losses)
    count.status = InventoryCount.Status.POSTED
    count.posted_by = user
    count.posted_at = timezone.now()
    count.gain_movement = gain_movement
    count.loss_movement = loss_movement
    count.save(update_fields=(
        "status", "posted_by", "posted_at", "gain_movement", "loss_movement", "updated_at",
    ))
    return count
