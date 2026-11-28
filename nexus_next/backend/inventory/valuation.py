"""IAS 2 lower-of-cost-and-NRV inventory assessment posting and reversal."""
from decimal import Decimal, ROUND_HALF_UP

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from accounting.models import AccountRole, JournalEntry, JournalLine
from accounting.services import get_account_for_role, post_journal, reverse_operational_journal
from companies.access import ensure_fiscal_period_open, resolve_warehouse_branch
from companies.models import FiscalYear
from .models import (
    InventoryValuationAssessment, InventoryValuationAssessmentLine,
    InventoryValuationLayerAdjustment, Product, StockBalance, StockCostLayer, StockMovement, Warehouse,
)


def _previous_layer_original_cost(layer):
    previous = InventoryValuationLayerAdjustment.objects.filter(
        layer=layer, line__assessment__status=InventoryValuationAssessment.Status.POSTED,
    ).order_by("-line__assessment__assessment_date", "-pk").first()
    if previous:
        return previous.original_unit_cost
    # A transfer or supplier-return reversal creates a child layer. Follow its
    # lineage so an earlier NRV write-down does not become the new historical cost.
    if layer.parent_layer_id:
        return _previous_layer_original_cost(layer.parent_layer)
    return layer.unit_cost


@transaction.atomic
def post_inventory_valuation_assessment(assessment_id, user):
    assessment = InventoryValuationAssessment.objects.select_for_update(of=("self",)).select_related(
        "company", "company__base_currency", "product", "warehouse",
    ).get(pk=assessment_id)
    if assessment.status != InventoryValuationAssessment.Status.DRAFT:
        raise ValidationError("تقييم المخزون مرحّل بالفعل أو غير قابل للترحيل.")
    if not assessment.product.is_active or assessment.product.product_type != "goods":
        raise ValidationError("لا يمكن تقييم صنف موقوف أو غير مخزني.")
    if not assessment.warehouse.is_active:
        raise ValidationError("لا يمكن تقييم مخزن موقوف.")
    # Match stock-posting lock order: product, warehouse, then balances/layers.
    Product.objects.select_for_update().get(pk=assessment.product_id)
    Warehouse.objects.select_for_update().get(pk=assessment.warehouse_id)

    year = FiscalYear.objects.filter(
        company=assessment.company, status=FiscalYear.Status.OPEN,
        start_date__lte=assessment.assessment_date, end_date__gte=assessment.assessment_date,
    ).order_by("-start_date").first()
    if not year:
        raise ValidationError("لا توجد سنة مالية مفتوحة تشمل تاريخ تقييم المخزون.")
    try:
        ensure_fiscal_period_open(year, assessment.assessment_date)
    except Exception as exc:
        raise ValidationError(str(exc)) from exc

    if StockMovement.objects.filter(
        company=assessment.company, status=StockMovement.Status.DRAFT,
    ).filter(
        Q(source_warehouse=assessment.warehouse) | Q(destination_warehouse=assessment.warehouse),
        lines__product=assessment.product,
    ).exists():
        raise ValidationError("يوجد مستند حركة مخزنية مسودة للصنف والمخزن؛ رحّله أو ألغِه قبل التقييم.")
    if StockMovement.objects.filter(
        company=assessment.company, status=StockMovement.Status.POSTED,
        movement_date__gt=assessment.assessment_date,
    ).filter(
        Q(source_warehouse=assessment.warehouse) | Q(destination_warehouse=assessment.warehouse),
        lines__product=assessment.product,
    ).exists():
        raise ValidationError("تاريخ التقييم أسبق من حركة مخزنية مرحلة؛ استخدم تاريخًا لاحقًا أو عالج الحركة أولًا.")
    if InventoryValuationAssessment.objects.filter(
        company=assessment.company, product=assessment.product, warehouse=assessment.warehouse,
        status=InventoryValuationAssessment.Status.POSTED,
        assessment_date__gt=assessment.assessment_date,
    ).exists():
        raise ValidationError("يوجد تقييم أحدث للصنف والمخزن؛ لا يمكن إدخال تقييم بتاريخ أقدم منه.")

    balances = list(StockBalance.objects.select_for_update().filter(
        product=assessment.product, warehouse=assessment.warehouse, quantity__gt=0,
    ).order_by("pk"))
    if not balances:
        raise ValidationError("لا يوجد رصيد مخزني موجب للصنف في المخزن المحدد.")
    layers = list(StockCostLayer.objects.select_for_update().filter(
        product=assessment.product, warehouse=assessment.warehouse, quantity_remaining__gt=0,
    ).order_by("pk"))
    layers_by_balance = {}
    for layer in layers:
        layers_by_balance.setdefault((layer.location_id, layer.expiry_date), []).append(layer)

    currency = assessment.company.base_currency
    quantum = Decimal("1").scaleb(-currency.decimal_places)
    before_total = Decimal("0")
    after_total = Decimal("0")
    quantity_total = Decimal("0")
    for balance in balances:
        balance_layers = layers_by_balance.get((balance.location_id, balance.expiry_date), [])
        tracked = sum((layer.quantity_remaining for layer in balance_layers), Decimal("0"))
        if tracked != balance.quantity or not balance_layers:
            raise ValidationError(
                f"طبقات تكلفة الصنف {assessment.product.code} لا تطابق الرصيد في أحد أبعاد المخزن؛ أوقفنا التقييم لحماية التكلفة."
            )
        previous_value = balance.inventory_value
        original_layer_value = sum((layer.quantity_remaining * _previous_layer_original_cost(layer) for layer in balance_layers), Decimal("0"))
        current_layer_value = sum((layer.quantity_remaining * layer.unit_cost for layer in balance_layers), Decimal("0"))
        if abs(current_layer_value - previous_value) > Decimal("0.0001"):
            raise ValidationError(f"طبقات تكلفة الصنف {assessment.product.code} لا تطابق القيمة الدفترية؛ أوقفنا تقييم NRV.")
        original_average = (original_layer_value / balance.quantity).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
        if assessment.product.costing_method == "fifo":
            layer_targets = []
            for layer in balance_layers:
                original_cost = _previous_layer_original_cost(layer)
                target_cost = min(original_cost, assessment.net_realizable_value_per_unit)
                layer_targets.append((layer, original_cost, target_cost))
            raw_after = sum((layer.quantity_remaining * target for layer, _original, target in layer_targets), Decimal("0"))
        else:
            target_average = min(original_average, assessment.net_realizable_value_per_unit)
            raw_after = balance.quantity * target_average
            layer_targets = []
            factor = (raw_after / original_layer_value) if original_layer_value > 0 else Decimal("0")
            for layer in balance_layers:
                original_cost = _previous_layer_original_cost(layer)
                layer_targets.append((layer, original_cost, original_cost * factor))
        adjusted_value = raw_after.quantize(quantum, rounding=ROUND_HALF_UP)
        layer_value = sum((layer.quantity_remaining * target for layer, _original, target in layer_targets), Decimal("0"))
        residual = adjusted_value - layer_value
        if residual:
            # Put currency-rounding residue on the layer carrying the largest value,
            # avoiding a negative unit cost when another layer is already fully written down.
            largest = max(
                layer_targets,
                key=lambda item: (item[0].quantity_remaining * item[2], item[0].quantity_remaining),
            )
            layer_targets = [
                (layer, original, target + residual / layer.quantity_remaining if layer.pk == largest[0].pk else target)
                for layer, original, target in layer_targets
            ]
            if any(target < 0 for _layer, _original, target in layer_targets):
                raise ValidationError("تعذر توزيع فرق التقريب على طبقات التكلفة دون قيمة سالبة.")
        adjusted_average = (adjusted_value / balance.quantity).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
        line = InventoryValuationAssessmentLine.objects.create(
            assessment=assessment, balance=balance, quantity=balance.quantity,
            original_average_cost=original_average, previous_average_cost=balance.average_cost,
            adjusted_average_cost=adjusted_average, previous_inventory_value=previous_value,
            adjusted_inventory_value=adjusted_value,
        )
        for layer, original_cost, target_cost in layer_targets:
            InventoryValuationLayerAdjustment.objects.create(
                line=line, layer=layer, original_unit_cost=original_cost,
                previous_unit_cost=layer.unit_cost, adjusted_unit_cost=target_cost,
                quantity_remaining=layer.quantity_remaining,
            )
            layer.unit_cost = target_cost
            layer.save(update_fields=("unit_cost",))
        balance.inventory_value = adjusted_value
        balance.average_cost = adjusted_average
        balance.save(update_fields=("inventory_value", "average_cost", "updated_at"))
        before_total += previous_value
        after_total += adjusted_value
        quantity_total += balance.quantity

    adjustment = (before_total - after_total).quantize(quantum, rounding=ROUND_HALF_UP)
    if adjustment == 0:
        raise ValidationError("قيمة المخزون لا تتجاوز صافي القيمة البيعية؛ لا توجد خسارة أو عكس تخفيض لإثباته.")
    branch = resolve_warehouse_branch(assessment.company, assessment.warehouse)
    if not branch or not branch.is_active:
        raise ValidationError("لا يمكن ترحيل تقييم مخزون دون فرع نشط للمخزن.")
    try:
        inventory_account = get_account_for_role(assessment.company, AccountRole.Role.INVENTORY)
        loss_account = get_account_for_role(assessment.company, AccountRole.Role.INVENTORY_ADJUSTMENT_LOSS)
    except DjangoValidationError as exc:
        detail = exc.message_dict if hasattr(exc, "message_dict") else exc.messages
        raise ValidationError(detail) from exc
    entry = JournalEntry.objects.create(
        company=assessment.company, branch=branch, fiscal_year=year,
        entry_number=f"INV-NRV-{assessment.pk}", entry_date=assessment.assessment_date,
        description=f"تقييم مخزون بصافي القيمة البيعية IAS 2 - {assessment.assessment_number}"[:500],
        source_type=JournalEntry.Source.INVENTORY, created_by=user,
    )
    if adjustment > 0:
        rows = [
            JournalLine(entry=entry, account=loss_account, description="خسارة تخفيض المخزون إلى NRV", debit=adjustment, credit=0),
            JournalLine(entry=entry, account=inventory_account, description="تخفيض القيمة الدفترية للمخزون", debit=0, credit=adjustment),
        ]
    else:
        recovery = abs(adjustment)
        rows = [
            JournalLine(entry=entry, account=inventory_account, description="عكس تخفيض المخزون بعد تحسن NRV", debit=recovery, credit=0),
            JournalLine(entry=entry, account=loss_account, description="عكس خسارة تخفيض المخزون", debit=0, credit=recovery),
        ]
    JournalLine.objects.bulk_create(rows)
    try:
        posted = post_journal(entry.pk, actor=user)
    except DjangoValidationError as exc:
        detail = exc.message_dict if hasattr(exc, "message_dict") else exc.messages
        raise ValidationError(detail) from exc
    assessment.quantity = quantity_total
    assessment.carrying_value_before = before_total
    assessment.carrying_value_after = after_total
    assessment.adjustment_amount = adjustment
    assessment.journal_entry = posted
    assessment.status = InventoryValuationAssessment.Status.POSTED
    assessment.posted_by = user
    assessment.posted_at = timezone.now()
    assessment.save(update_fields=(
        "quantity", "carrying_value_before", "carrying_value_after", "adjustment_amount",
        "journal_entry", "status", "posted_by", "posted_at",
    ))
    return assessment


@transaction.atomic
def reverse_inventory_valuation_assessment(assessment_id, user, entry_date):
    assessment = InventoryValuationAssessment.objects.select_for_update(of=("self",)).select_related(
        "company", "product", "warehouse", "journal_entry",
    ).get(pk=assessment_id)
    if assessment.status != InventoryValuationAssessment.Status.POSTED or not assessment.journal_entry_id:
        raise ValidationError("لا يمكن عكس إلا تقييم مخزون مرحّل.")
    if assessment.reversal_journal_entry_id:
        raise ValidationError("تم عكس تقييم المخزون من قبل.")
    if entry_date < assessment.assessment_date:
        raise ValidationError("تاريخ العكس لا يجوز أن يسبق تاريخ التقييم.")
    # Serialize reversal against stock postings touching this product/warehouse.
    Product.objects.select_for_update().get(pk=assessment.product_id)
    Warehouse.objects.select_for_update().get(pk=assessment.warehouse_id)
    if StockMovement.objects.filter(
        company=assessment.company, status=StockMovement.Status.POSTED,
        posted_at__gt=assessment.posted_at,
    ).filter(
        Q(source_warehouse=assessment.warehouse) | Q(destination_warehouse=assessment.warehouse),
        lines__product=assessment.product,
    ).exists():
        raise ValidationError("لا يمكن عكس التقييم بعد حدوث حركة مخزنية لاحقة؛ أنشئ تقييمًا جديدًا وفق NRV الحالي.")
    if InventoryValuationAssessment.objects.filter(
        company=assessment.company, product=assessment.product, warehouse=assessment.warehouse,
        status=InventoryValuationAssessment.Status.POSTED, posted_at__gt=assessment.posted_at,
    ).exists():
        raise ValidationError("لا يمكن عكس تقييم له تقييمات لاحقة؛ اعكس الأحدث أولًا.")
    lines = list(assessment.lines.select_for_update().select_related("balance").prefetch_related("layer_adjustments__layer"))
    for line in lines:
        for snapshot in line.layer_adjustments.all():
            snapshot.layer.unit_cost = snapshot.previous_unit_cost
            snapshot.layer.save(update_fields=("unit_cost",))
        line.balance.inventory_value = line.previous_inventory_value
        line.balance.average_cost = line.previous_average_cost
        line.balance.save(update_fields=("inventory_value", "average_cost", "updated_at"))
    try:
        reversal = reverse_operational_journal(
            assessment.journal_entry_id, user, entry_date, JournalEntry.Source.INVENTORY,
        )
    except DjangoValidationError as exc:
        detail = exc.message_dict if hasattr(exc, "message_dict") else exc.messages
        raise ValidationError(detail) from exc
    InventoryValuationAssessment.objects.filter(pk=assessment.pk).update(
        reversal_journal_entry=reversal,
        status=InventoryValuationAssessment.Status.REVERSED,
        reversed_at=timezone.now(),
    )
    assessment.refresh_from_db()
    return assessment
