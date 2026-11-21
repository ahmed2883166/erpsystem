from decimal import Decimal

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .models import StockBalance, StockMovement, StockMovementLine


@transaction.atomic
def post_stock_movement(movement_id, user):
    movement = StockMovement.objects.select_for_update().select_related(
        "company", "source_warehouse", "destination_warehouse"
    ).get(pk=movement_id)
    if movement.status != StockMovement.Status.DRAFT:
        raise ValidationError("الحركة مرحلة بالفعل أو غير قابلة للترحيل.")
    lines = list(movement.lines.select_related("product").order_by("product_id", "id"))
    if not lines:
        raise ValidationError("لا يمكن ترحيل حركة بدون أصناف.")

    for line in lines:
        product = line.product
        if product.company_id != movement.company_id or product.product_type != "goods":
            raise ValidationError("الحركة تحتوي على صنف غير مخزني أو تابع لشركة أخرى.")
        if movement.movement_type == StockMovement.MovementType.RECEIPT:
            warehouse_ids = [movement.destination_warehouse_id]
        elif movement.movement_type == StockMovement.MovementType.ISSUE:
            warehouse_ids = [movement.source_warehouse_id]
        elif movement.movement_type == StockMovement.MovementType.TRANSFER:
            warehouse_ids = sorted([movement.source_warehouse_id, movement.destination_warehouse_id])
        elif line.adjustment_direction == StockMovementLine.AdjustmentDirection.IN:
            warehouse_ids = [movement.destination_warehouse_id]
        else:
            warehouse_ids = [movement.source_warehouse_id]

        for warehouse_id in warehouse_ids:
            StockBalance.objects.get_or_create(product=product, warehouse_id=warehouse_id)
        locked = StockBalance.objects.select_for_update().filter(
            product=product, warehouse_id__in=warehouse_ids
        ).order_by("warehouse_id")
        balances = {balance.warehouse_id: balance for balance in locked}

        def receive(balance, quantity, unit_cost):
            amount = (quantity * unit_cost).quantize(Decimal("0.0001"))
            new_quantity = balance.quantity + quantity
            new_value = balance.inventory_value + amount
            balance.quantity = new_quantity
            balance.inventory_value = new_value
            balance.average_cost = (new_value / new_quantity).quantize(Decimal("0.000001"))
            balance.save(update_fields=("quantity", "inventory_value", "average_cost", "updated_at"))

        def issue(balance, quantity):
            if balance.quantity < quantity:
                raise ValidationError(
                    f"الرصيد غير كافٍ للصنف {product.code}. المتاح {balance.quantity} والمطلوب {quantity}."
                )
            actual_cost = balance.average_cost
            amount = (quantity * actual_cost).quantize(Decimal("0.0001"))
            balance.quantity -= quantity
            balance.inventory_value = max(Decimal("0"), balance.inventory_value - amount)
            if balance.quantity == 0:
                balance.inventory_value = Decimal("0")
                balance.average_cost = Decimal("0")
            else:
                balance.average_cost = (balance.inventory_value / balance.quantity).quantize(Decimal("0.000001"))
            balance.save(update_fields=("quantity", "inventory_value", "average_cost", "updated_at"))
            return actual_cost

        if movement.movement_type == StockMovement.MovementType.RECEIPT:
            receive(balances[movement.destination_warehouse_id], line.quantity, line.unit_cost)
        elif movement.movement_type == StockMovement.MovementType.ISSUE:
            line.unit_cost = issue(balances[movement.source_warehouse_id], line.quantity)
            line.save(update_fields=("unit_cost",))
        elif movement.movement_type == StockMovement.MovementType.TRANSFER:
            actual_cost = issue(balances[movement.source_warehouse_id], line.quantity)
            receive(balances[movement.destination_warehouse_id], line.quantity, actual_cost)
            line.unit_cost = actual_cost
            line.save(update_fields=("unit_cost",))
        elif movement.movement_type == StockMovement.MovementType.ADJUSTMENT:
            if line.adjustment_direction == StockMovementLine.AdjustmentDirection.IN:
                receive(balances[movement.destination_warehouse_id], line.quantity, line.unit_cost)
            else:
                line.unit_cost = issue(balances[movement.source_warehouse_id], line.quantity)
                line.save(update_fields=("unit_cost",))

    movement.status = StockMovement.Status.POSTED
    movement.posted_by = user
    movement.posted_at = timezone.now()
    movement.save(update_fields=("status", "posted_by", "posted_at"))
    return movement
