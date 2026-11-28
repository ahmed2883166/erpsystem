"""خدمات ترحيل حركات المخزون وتحديث الأرصدة وطبقات التكلفة والقيود الناتجة."""

from decimal import Decimal

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .models import InventoryCount, Product, StockBalance, StockCostConsumption, StockCostLayer, StockMovement, StockMovementLine, Warehouse


@transaction.atomic
def post_stock_movement(movement_id, user, *, inventory_count_id=None):
    """ترحيل حركة المخزون وتحديث الأرصدة وطبقات التكلفة وأثرها المحاسبي."""
    # Lock only the movement row; nullable warehouse joins cannot be locked in PostgreSQL.
    movement = StockMovement.objects.select_for_update().select_related("company").get(pk=movement_id)
    if movement.status != StockMovement.Status.DRAFT:
        raise ValidationError("الحركة مرحلة بالفعل أو غير قابلة للترحيل.")

    # Stock quantities and their accounting consequences must never be posted
    # into a closed fiscal year/month. Check every affected branch before writes.
    from companies.access import ensure_fiscal_period_open, resolve_warehouse_branch
    from companies.models import FiscalYear

    affected_warehouses = [
        warehouse for warehouse in (movement.source_warehouse, movement.destination_warehouse)
        if warehouse is not None
    ]
    for warehouse in affected_warehouses:
        branch = resolve_warehouse_branch(movement.company, warehouse)
        if not branch or not branch.is_active:
            raise ValidationError("لا يمكن ترحيل حركة مخزون دون فرع نشط مرتبط بالمخزن.")
        fiscal_year = FiscalYear.objects.filter(
            company=movement.company, status=FiscalYear.Status.OPEN,
            start_date__lte=movement.movement_date, end_date__gte=movement.movement_date,
        ).order_by("-start_date").first()
        if not fiscal_year:
            raise ValidationError("لا توجد سنة مالية مفتوحة تشمل تاريخ حركة المخزون.")
        try:
            ensure_fiscal_period_open(fiscal_year, movement.movement_date)
        except DjangoValidationError as exc:
            raise ValidationError(exc.messages) from exc
    lines = list(movement.lines.select_related("product").order_by("product_id", "id"))
    if not lines:
        raise ValidationError("لا يمكن ترحيل حركة بدون أصناف.")

    # Serialize posting per product, not merely per movement. Locking the product
    # rows in a stable order also protects the get_or_create path for a balance
    # row that does not exist yet (a row lock cannot protect a missing row).
    product_ids = sorted({line.product_id for line in lines})
    locked_products = list(
        Product.objects.select_for_update().select_related("unit")
        .filter(pk__in=product_ids).order_by("pk")
    )
    products_by_id = {product.pk: product for product in locked_products}
    if len(products_by_id) != len(product_ids):
        raise ValidationError("يوجد صنف غير موجود ضمن بنود الحركة.")

    warehouse_ids = sorted({
        warehouse_id for warehouse_id in (
            movement.source_warehouse_id, movement.destination_warehouse_id,
        ) if warehouse_id is not None
    })
    # Warehouse locks serialize postings with stock-count snapshots and with
    # other movements that touch the same warehouse.
    list(Warehouse.objects.select_for_update().filter(pk__in=warehouse_ids).order_by("pk"))
    active_counts = InventoryCount.objects.filter(
        warehouse_id__in=warehouse_ids,
        status=InventoryCount.Status.DRAFT,
    )
    if inventory_count_id is not None:
        active_counts = active_counts.exclude(pk=inventory_count_id)
    if active_counts.exists():
        raise ValidationError(
            "لا يمكن ترحيل حركة أثناء وجود جرد فعلي مفتوح على المخزن. أكمل الجرد أو ألغِه أولاً."
        )

    movement_type = movement.movement_type
    source_required = movement_type in (
        StockMovement.MovementType.ISSUE,
        StockMovement.MovementType.SUPPLIER_RETURN,
        StockMovement.MovementType.SALES_RETURN_REVERSAL,
    )
    destination_required = movement_type in (
        StockMovement.MovementType.RECEIPT,
        StockMovement.MovementType.SUPPLIER_RETURN_REVERSAL,
    )
    if source_required and not movement.source_warehouse_id:
        raise ValidationError("حدد مخزن المصدر لهذه الحركة.")
    if destination_required and not movement.destination_warehouse_id:
        raise ValidationError("حدد مخزن الوجهة لهذه الحركة.")
    if movement_type == StockMovement.MovementType.TRANSFER:
        if not movement.source_warehouse_id or not movement.destination_warehouse_id:
            raise ValidationError("التحويل يتطلب مخزن مصدر ومخزن وجهة.")
        if movement.source_warehouse_id == movement.destination_warehouse_id:
            raise ValidationError("لا يمكن التحويل إلى نفس المخزن.")
    if movement_type == StockMovement.MovementType.ADJUSTMENT:
        if not movement.adjustment_reason:
            raise ValidationError("حدد سبب التسوية قبل الترحيل.")
        directions = {line.adjustment_direction for line in lines}
        if len(directions) != 1 or "" in directions:
            raise ValidationError("حدد اتجاهًا موحدًا لكل بنود التسوية.")
        direction = next(iter(directions))
        if (
            movement.adjustment_reason in (
                StockMovement.AdjustmentReason.DAMAGE,
                StockMovement.AdjustmentReason.EXPIRED,
                StockMovement.AdjustmentReason.WRITE_OFF,
            )
            and direction != StockMovementLine.AdjustmentDirection.OUT
        ):
            raise ValidationError("أسباب التلف والانتهاء والشطب يجب أن تكون تسوية عجز.")
        if direction == StockMovementLine.AdjustmentDirection.IN:
            if not movement.destination_warehouse_id or movement.source_warehouse_id:
                raise ValidationError("زيادة الجرد تتطلب مخزن وجهة فقط.")
        elif direction == StockMovementLine.AdjustmentDirection.OUT:
            if not movement.source_warehouse_id or movement.destination_warehouse_id:
                raise ValidationError("عجز الجرد يتطلب مخزن مصدر فقط.")
        else:
            raise ValidationError("اتجاه التسوية غير صحيح.")

    for warehouse in (movement.source_warehouse, movement.destination_warehouse):
        if warehouse and (warehouse.company_id != movement.company_id or not warehouse.is_active):
            raise ValidationError("مخزن الحركة غير نشط أو لا يتبع الشركة.")

    for line in lines:
        line.product = products_by_id[line.product_id]
        product = line.product
        if not product.is_active:
            raise ValidationError(f"الصنف {product.code} موقوف ولا يمكن ترحيل حركة عليه.")
        if not product.unit.is_active:
            raise ValidationError(f"وحدة قياس الصنف {product.code} موقوفة.")
        quantity_quantum = Decimal("1").scaleb(-product.unit.decimal_places)
        if line.quantity != line.quantity.quantize(quantity_quantum):
            raise ValidationError(
                f"كمية الصنف {product.code} تتجاوز دقة وحدة القياس "
                f"({product.unit.decimal_places} منازل عشرية)."
            )
        if line.source_location_id and (
            not movement.source_warehouse_id
            or line.source_location.warehouse_id != movement.source_warehouse_id
            or not line.source_location.is_active
        ):
            raise ValidationError(f"موقع المصدر للصنف {product.code} غير صالح.")
        if line.destination_location_id and (
            not movement.destination_warehouse_id
            or line.destination_location.warehouse_id != movement.destination_warehouse_id
            or not line.destination_location.is_active
        ):
            raise ValidationError(f"موقع الوجهة للصنف {product.code} غير صالح.")
        if product.track_expiry and not line.expiry_date:
            raise ValidationError(f"تاريخ الصلاحية مطلوب للصنف {product.code}.")
        if not product.track_expiry and line.expiry_date:
            raise ValidationError(f"الصنف {product.code} لا يتطلب تتبع الصلاحية.")
        if (
            product.track_expiry
            and line.expiry_date
            and line.expiry_date < movement.movement_date
            and movement_type in (StockMovement.MovementType.ISSUE, StockMovement.MovementType.TRANSFER)
        ):
            raise ValidationError(f"لا يمكن صرف أو تحويل تشغيلة منتهية الصلاحية للصنف {product.code}.")
        if movement_type != StockMovement.MovementType.ADJUSTMENT and line.adjustment_direction:
            raise ValidationError("اتجاه التسوية لا يستخدم إلا في حركات التسوية.")

    for line in lines:
        product = line.product
        if product.company_id != movement.company_id or product.product_type != Product.ProductType.GOODS:
            raise ValidationError("الحركة تحتوي على صنف غير مخزني أو تابع لشركة أخرى.")
        if product.track_expiry and not line.expiry_date:
            raise ValidationError(f"تاريخ الصلاحية مطلوب للصنف {product.code}.")

        source_key = (movement.source_warehouse_id, line.source_location_id, line.expiry_date)
        destination_key = (movement.destination_warehouse_id, line.destination_location_id, line.expiry_date)
        if movement.movement_type in (StockMovement.MovementType.RECEIPT, StockMovement.MovementType.SUPPLIER_RETURN_REVERSAL):
            balance_keys = [destination_key]
        elif movement.movement_type in (StockMovement.MovementType.ISSUE, StockMovement.MovementType.SUPPLIER_RETURN, StockMovement.MovementType.SALES_RETURN_REVERSAL):
            balance_keys = [source_key]
        elif movement.movement_type == StockMovement.MovementType.TRANSFER:
            balance_keys = [source_key, destination_key]
        elif line.adjustment_direction == StockMovementLine.AdjustmentDirection.IN:
            balance_keys = [destination_key]
        else:
            balance_keys = [source_key]

        def key_order(key):
            """تنفيذ مسؤولية الدالة key_order ضمن وحدة services.py مع الحفاظ على قواعد التحقق والتعامل مع البيانات."""
            return (key[0] or 0, key[1] or 0, key[2].isoformat() if key[2] else "")

        balance_ids = []
        for warehouse_id, location_id, expiry_date in sorted(set(balance_keys), key=key_order):
            balance, _ = StockBalance.objects.get_or_create(
                product=product, warehouse_id=warehouse_id, location_id=location_id, expiry_date=expiry_date
            )
            balance_ids.append(balance.pk)
        locked = StockBalance.objects.select_for_update().filter(pk__in=balance_ids).order_by("id")
        balances = {(b.warehouse_id, b.location_id, b.expiry_date): b for b in locked}

        def receive(balance, quantity, unit_cost, source_line, product=product, parent_layer=None):
            """تنفيذ مسؤولية الدالة receive ضمن وحدة services.py مع الحفاظ على قواعد التحقق والتعامل مع البيانات."""
            amount = (quantity * unit_cost).quantize(Decimal("0.0001"))
            new_quantity = balance.quantity + quantity
            new_value = balance.inventory_value + amount
            balance.quantity = new_quantity
            balance.inventory_value = new_value
            balance.average_cost = (new_value / new_quantity).quantize(Decimal("0.000001"))
            balance.save(update_fields=("quantity", "inventory_value", "average_cost", "updated_at"))
            if product.costing_method in (Product.CostingMethod.FIFO, Product.CostingMethod.WEIGHTED_AVERAGE):
                StockCostLayer.objects.create(
                    product=product, warehouse=balance.warehouse, location=balance.location,
                    expiry_date=balance.expiry_date, source_line=source_line,
                    root_source_line_id=(parent_layer.root_source_line_id if parent_layer else source_line.pk),
                    parent_layer=parent_layer,
                    quantity_remaining=quantity, unit_cost=unit_cost,
                )

        def issue(balance, quantity, movement_line, product=product, source_root_line_id=None):
            """تنفيذ مسؤولية الدالة issue ضمن وحدة services.py مع الحفاظ على قواعد التحقق والتعامل مع البيانات."""
            if balance.quantity < quantity:
                raise ValidationError(
                    f"الرصيد غير كافٍ للصنف {product.code}. المتاح {balance.quantity} والمطلوب {quantity}."
                )
            cost_parts = []
            layer_query = StockCostLayer.objects.select_for_update().filter(
                product=product, warehouse=balance.warehouse, location=balance.location,
                expiry_date=balance.expiry_date, quantity_remaining__gt=0,
                source_line__movement__status=StockMovement.Status.POSTED,
            )
            if source_root_line_id is not None:
                layer_query = layer_query.filter(root_source_line_id=source_root_line_id)
            layers = list(layer_query.order_by(
                "source_line__movement__movement_date", "source_line__movement_id", "source_line_id", "id"
            ))
            if product.costing_method == Product.CostingMethod.FIFO:
                remaining = quantity
                for layer in layers:
                    if remaining <= 0:
                        break
                    consumed = min(layer.quantity_remaining, remaining)
                    layer.quantity_remaining -= consumed
                    layer.save(update_fields=("quantity_remaining",))
                    StockCostConsumption.objects.create(
                        movement_line=movement_line, source_layer=layer,
                        quantity=consumed, unit_cost=layer.unit_cost,
                    )
                    cost_parts.append((consumed, layer.unit_cost, layer))
                    remaining -= consumed
                if remaining > 0:
                    raise ValidationError(
                        f"طبقات تكلفة FIFO غير مكتملة للصنف {product.code}; المتبقي {remaining}."
                    )
            else:
                tracked_quantity = sum((layer.quantity_remaining for layer in layers), Decimal("0"))
                if source_root_line_id is not None and tracked_quantity < quantity:
                    raise ValidationError(
                        f"طبقات تكلفة مصدر الفاتورة غير كافية للصنف {product.code}; "
                        f"المتاح من المصدر {tracked_quantity} والمطلوب {quantity}."
                    )
                tracked_to_consume = (
                    quantity if source_root_line_id is not None else min(
                        tracked_quantity,
                        quantity if quantity == balance.quantity else
                        (quantity * tracked_quantity / balance.quantity).quantize(Decimal("0.0001")),
                    )
                )
                remaining_tracked = tracked_to_consume
                for index, layer in enumerate(layers):
                    if remaining_tracked <= 0:
                        break
                    if index == len(layers) - 1:
                        consumed = remaining_tracked
                    else:
                        consumed = min(
                            layer.quantity_remaining,
                            (tracked_to_consume * layer.quantity_remaining / tracked_quantity).quantize(Decimal("0.0001")),
                        )
                    if consumed <= 0:
                        continue
                    layer.quantity_remaining -= consumed
                    layer.save(update_fields=("quantity_remaining",))
                    unit_cost = layer.unit_cost if source_root_line_id is not None else balance.average_cost
                    StockCostConsumption.objects.create(
                        movement_line=movement_line, source_layer=layer,
                        quantity=consumed, unit_cost=unit_cost,
                    )
                    cost_parts.append((consumed, unit_cost, layer))
                    remaining_tracked -= consumed
                untracked_quantity = quantity - tracked_to_consume
                if untracked_quantity > 0:
                    cost_parts.append((untracked_quantity, balance.average_cost, None))

            amount = sum(
                (part_quantity * unit_cost).quantize(Decimal("0.0001"))
                for part_quantity, unit_cost, _source_layer in cost_parts
            )
            balance.quantity -= quantity
            if amount > balance.inventory_value + Decimal("0.0001"):
                raise ValidationError(
                    f"قيمة طبقات التكلفة للصنف {product.code} تتجاوز قيمة رصيد المخزون؛ "
                    "أوقفنا الصرف بدلًا من إخفاء فرق التقييم."
                )
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
        elif movement.movement_type == StockMovement.MovementType.SUPPLIER_RETURN_REVERSAL:
            return_line = getattr(line, "supplier_return_reversal_line", None)
            if return_line is None:
                raise ValidationError("حركة عكس المرتجع غير مرتبطة ببند مرتجع مورد أصلي.")
            if product.costing_method not in (Product.CostingMethod.FIFO, Product.CostingMethod.WEIGHTED_AVERAGE):
                raise ValidationError("عكس مرتجع المورد يتطلب استمرار تتبع تكلفة FIFO أو المتوسط المرجح.")
            consumptions = list(StockCostConsumption.objects.filter(
                movement_line=return_line.movement_line,
            ).select_related("source_layer").order_by("id"))
            restored_quantity = sum((item.quantity for item in consumptions), Decimal("0"))
            if restored_quantity != line.quantity or not consumptions:
                raise ValidationError("سجل استهلاك طبقات تكلفة المرتجع الأصلي غير مكتمل؛ أوقفنا العكس لحماية المخزون.")
            for item in consumptions:
                receive(
                    balances[destination_key], item.quantity, item.unit_cost, line,
                    parent_layer=item.source_layer,
                )
            line.cost_amount = sum(
                (item.quantity * item.unit_cost).quantize(Decimal("0.0001")) for item in consumptions
            )
            line.unit_cost = line.cost_amount / line.quantity
            line.save(update_fields=("unit_cost", "cost_amount"))
        elif movement.movement_type in (StockMovement.MovementType.ISSUE, StockMovement.MovementType.SUPPLIER_RETURN, StockMovement.MovementType.SALES_RETURN_REVERSAL):
            source_root_line_id = None
            if movement.movement_type == StockMovement.MovementType.SUPPLIER_RETURN:
                source_root_line_id = line.supplier_return_line.invoice_line.receipt_line_id
                if product.costing_method not in (Product.CostingMethod.FIFO, Product.CostingMethod.WEIGHTED_AVERAGE):
                    raise ValidationError("مرتجع المورد يتطلب تتبع تكلفة FIFO أو المتوسط المرجح.")
            elif movement.movement_type == StockMovement.MovementType.SALES_RETURN_REVERSAL:
                if not line.reversal_of_line_id:
                    raise ValidationError("عكس مردود المبيعات يجب أن يرتبط ببند الاستلام الأصلي.")
                if product.costing_method not in (Product.CostingMethod.FIFO, Product.CostingMethod.WEIGHTED_AVERAGE):
                    raise ValidationError("عكس مردود المبيعات يتطلب استمرار تتبع تكلفة FIFO أو المتوسط المرجح.")
                source_root_line_id = line.reversal_of_line_id
            cost_parts = issue(
                balances[source_key], line.quantity, line,
                source_root_line_id=source_root_line_id,
            )
            line.cost_amount = sum((qty * cost).quantize(Decimal("0.0001")) for qty, cost, _source_layer in cost_parts)
            line.unit_cost = line.cost_amount / line.quantity
            line.save(update_fields=("unit_cost", "cost_amount"))
        elif movement.movement_type == StockMovement.MovementType.TRANSFER:
            cost_parts = issue(balances[source_key], line.quantity, line)
            for qty, cost, source_layer in cost_parts:
                receive(balances[destination_key], qty, cost, line, parent_layer=source_layer)
            line.cost_amount = sum((qty * cost).quantize(Decimal("0.0001")) for qty, cost, _source_layer in cost_parts)
            line.unit_cost = line.cost_amount / line.quantity
            line.save(update_fields=("unit_cost", "cost_amount"))
        elif movement.movement_type == StockMovement.MovementType.ADJUSTMENT:
            if line.adjustment_direction == StockMovementLine.AdjustmentDirection.IN:
                line.cost_amount = (line.quantity * line.unit_cost).quantize(Decimal("0.0001"))
                line.save(update_fields=("cost_amount",))
                receive(balances[destination_key], line.quantity, line.unit_cost, line)
            else:
                cost_parts = issue(balances[source_key], line.quantity, line)
                line.cost_amount = sum((qty * cost).quantize(Decimal("0.0001")) for qty, cost, _source_layer in cost_parts)
                line.unit_cost = line.cost_amount / line.quantity
                line.save(update_fields=("unit_cost", "cost_amount"))

    if movement.movement_type not in (StockMovement.MovementType.SUPPLIER_RETURN, StockMovement.MovementType.SUPPLIER_RETURN_REVERSAL):
        from .posting import post_movement_journals
        post_movement_journals(movement, user)

    movement.status = StockMovement.Status.POSTED
    movement.posted_by = user
    movement.posted_at = timezone.now()
    movement.save(update_fields=("status", "posted_by", "posted_at"))
    return movement
