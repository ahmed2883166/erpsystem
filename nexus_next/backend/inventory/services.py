from decimal import Decimal

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .models import Product, StockBalance, StockCostLayer, StockMovement, StockMovementLine


@transaction.atomic
def post_stock_movement(movement_id, user):
    # Lock only the movement row; nullable warehouse joins cannot be locked in PostgreSQL.
    movement = StockMovement.objects.select_for_update().get(pk=movement_id)
    if movement.status != StockMovement.Status.DRAFT:
        raise ValidationError("الحركة مرحلة بالفعل أو غير قابلة للترحيل.")
    lines = list(movement.lines.select_related("product").order_by("product_id", "id"))
    if not lines:
        raise ValidationError("لا يمكن ترحيل حركة بدون أصناف.")

    for line in lines:
        product = line.product
        if product.company_id != movement.company_id or product.product_type != Product.ProductType.GOODS:
            raise ValidationError("الحركة تحتوي على صنف غير مخزني أو تابع لشركة أخرى.")
        if product.track_expiry and not line.expiry_date:
            raise ValidationError(f"تاريخ الصلاحية مطلوب للصنف {product.code}.")

        source_key = (movement.source_warehouse_id, line.source_location_id, line.expiry_date)
        destination_key = (movement.destination_warehouse_id, line.destination_location_id, line.expiry_date)
        if movement.movement_type == StockMovement.MovementType.RECEIPT:
            balance_keys = [destination_key]
        elif movement.movement_type == StockMovement.MovementType.ISSUE:
            balance_keys = [source_key]
        elif movement.movement_type == StockMovement.MovementType.TRANSFER:
            balance_keys = [source_key, destination_key]
        elif line.adjustment_direction == StockMovementLine.AdjustmentDirection.IN:
            balance_keys = [destination_key]
        else:
            balance_keys = [source_key]

        def key_order(key):
            return (key[0] or 0, key[1] or 0, key[2].isoformat() if key[2] else "")

        balance_ids = []
        for warehouse_id, location_id, expiry_date in sorted(set(balance_keys), key=key_order):
            balance, _ = StockBalance.objects.get_or_create(
                product=product, warehouse_id=warehouse_id, location_id=location_id, expiry_date=expiry_date
            )
            balance_ids.append(balance.pk)
        locked = StockBalance.objects.select_for_update().filter(pk__in=balance_ids).order_by("id")
        balances = {(b.warehouse_id, b.location_id, b.expiry_date): b for b in locked}

        def receive(balance, quantity, unit_cost, source_line):
            amount = (quantity * unit_cost).quantize(Decimal("0.0001"))
            new_quantity = balance.quantity + quantity
            new_value = balance.inventory_value + amount
            balance.quantity = new_quantity
            balance.inventory_value = new_value
            balance.average_cost = (new_value / new_quantity).quantize(Decimal("0.000001"))
            balance.save(update_fields=("quantity", "inventory_value", "average_cost", "updated_at"))
            if product.costing_method == Product.CostingMethod.FIFO:
                StockCostLayer.objects.create(
                    product=product, warehouse=balance.warehouse, location=balance.location,
                    expiry_date=balance.expiry_date, source_line=source_line,
                    quantity_remaining=quantity, unit_cost=unit_cost,
                )

        def issue(balance, quantity):
            if balance.quantity < quantity:
                raise ValidationError(
                    f"الرصيد غير كافٍ للصنف {product.code}. المتاح {balance.quantity} والمطلوب {quantity}."
                )
            cost_parts = []
            if product.costing_method == Product.CostingMethod.FIFO:
                remaining = quantity
                layers = StockCostLayer.objects.select_for_update().filter(
                    product=product, warehouse=balance.warehouse, location=balance.location,
                    expiry_date=balance.expiry_date, quantity_remaining__gt=0,
                ).order_by("created_at", "id")
                for layer in layers:
                    if remaining <= 0:
                        break
                    consumed = min(layer.quantity_remaining, remaining)
                    layer.quantity_remaining -= consumed
                    layer.save(update_fields=("quantity_remaining",))
                    cost_parts.append((consumed, layer.unit_cost))
                    remaining -= consumed
                if remaining > 0:
                    raise ValidationError(
                        f"طبقات تكلفة FIFO غير مكتملة للصنف {product.code}; المتبقي {remaining}."
                    )
            else:
                cost_parts.append((quantity, balance.average_cost))

            amount = sum(
                (part_quantity * unit_cost).quantize(Decimal("0.0001"))
                for part_quantity, unit_cost in cost_parts
            )
            balance.quantity -= quantity
            balance.inventory_value = max(Decimal("0"), balance.inventory_value - amount)
            if balance.quantity == 0:
                balance.inventory_value = Decimal("0")
                balance.average_cost = Decimal("0")
            else:
                balance.average_cost = (balance.inventory_value / balance.quantity).quantize(Decimal("0.000001"))
            balance.save(update_fields=("quantity", "inventory_value", "average_cost", "updated_at"))
            return cost_parts

        if movement.movement_type == StockMovement.MovementType.RECEIPT:
            line.cost_amount = (line.quantity * line.unit_cost).quantize(Decimal("0.0001"))
            line.save(update_fields=("cost_amount",))
            receive(balances[destination_key], line.quantity, line.unit_cost, line)
        elif movement.movement_type == StockMovement.MovementType.ISSUE:
            cost_parts = issue(balances[source_key], line.quantity)
            line.cost_amount = sum((qty * cost).quantize(Decimal("0.0001")) for qty, cost in cost_parts)
            line.unit_cost = line.cost_amount / line.quantity
            line.save(update_fields=("unit_cost", "cost_amount"))
        elif movement.movement_type == StockMovement.MovementType.TRANSFER:
            cost_parts = issue(balances[source_key], line.quantity)
            for qty, cost in cost_parts:
                receive(balances[destination_key], qty, cost, line)
            line.cost_amount = sum((qty * cost).quantize(Decimal("0.0001")) for qty, cost in cost_parts)
            line.unit_cost = line.cost_amount / line.quantity
            line.save(update_fields=("unit_cost", "cost_amount"))
        elif movement.movement_type == StockMovement.MovementType.ADJUSTMENT:
            if line.adjustment_direction == StockMovementLine.AdjustmentDirection.IN:
                line.cost_amount = (line.quantity * line.unit_cost).quantize(Decimal("0.0001"))
                line.save(update_fields=("cost_amount",))
                receive(balances[destination_key], line.quantity, line.unit_cost, line)
            else:
                cost_parts = issue(balances[source_key], line.quantity)
                line.cost_amount = sum((qty * cost).quantize(Decimal("0.0001")) for qty, cost in cost_parts)
                line.unit_cost = line.cost_amount / line.quantity
                line.save(update_fields=("unit_cost", "cost_amount"))

    from .posting import post_movement_journals
    post_movement_journals(movement, user)

    movement.status = StockMovement.Status.POSTED
    movement.posted_by = user
    movement.posted_at = timezone.now()
    movement.save(update_fields=("status", "posted_by", "posted_at"))
    return movement
