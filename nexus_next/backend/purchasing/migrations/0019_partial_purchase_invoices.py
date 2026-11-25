import django.core.validators
import django.db.models.deletion
from decimal import Decimal
from django.db import migrations, models
from django.db.models import Count


def backfill_billed_quantity(apps, schema_editor):
    PurchaseInvoiceLine = apps.get_model("purchasing", "PurchaseInvoiceLine")
    database = schema_editor.connection.alias
    rows = PurchaseInvoiceLine.objects.using(database).select_related("receipt_line").iterator(chunk_size=1000)
    batch = []
    manager = PurchaseInvoiceLine.objects.using(database)
    for row in rows:
        row.billed_quantity = row.receipt_line.quantity
        batch.append(row)
        if len(batch) >= 1000:
            manager.bulk_update(batch, ("billed_quantity",), batch_size=1000)
            batch.clear()
    if batch:
        manager.bulk_update(batch, ("billed_quantity",), batch_size=1000)


def ensure_legacy_one_to_one_can_be_restored(apps, schema_editor):
    PurchaseInvoice = apps.get_model("purchasing", "PurchaseInvoice")
    PurchaseInvoiceLine = apps.get_model("purchasing", "PurchaseInvoiceLine")
    database = schema_editor.connection.alias
    duplicate_receipts = PurchaseInvoice.objects.using(database).values(
        "receipt_id"
    ).annotate(total=Count("pk")).filter(total__gt=1).exists()
    duplicate_receipt_lines = PurchaseInvoiceLine.objects.using(database).values(
        "receipt_line_id"
    ).annotate(total=Count("pk")).filter(total__gt=1).exists()
    if duplicate_receipts or duplicate_receipt_lines:
        raise RuntimeError(
            "Cannot reverse partial-invoice migration: multiple invoices now reference "
            "the same receipt or receipt line. Keep the forward schema to avoid data loss."
        )


class Migration(migrations.Migration):
    dependencies = [
        ("purchasing", "0018_purchase_receipt_grni_journal"),
    ]

    operations = [
        migrations.AlterField(
            model_name="purchaseinvoice",
            name="receipt",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="invoices",
                to="purchasing.purchasereceipt",
            ),
        ),
        migrations.AlterField(
            model_name="purchaseinvoiceline",
            name="receipt_line",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="purchase_invoice_lines",
                to="inventory.stockmovementline",
            ),
        ),
        migrations.AddField(
            model_name="purchaseinvoiceline",
            name="billed_quantity",
            field=models.DecimalField(
                max_digits=18, decimal_places=4, null=True,
                validators=[django.core.validators.MinValueValidator(Decimal("0.0001"))],
            ),
        ),
        migrations.RunPython(backfill_billed_quantity, ensure_legacy_one_to_one_can_be_restored),
        migrations.AlterField(
            model_name="purchaseinvoiceline",
            name="billed_quantity",
            field=models.DecimalField(
                max_digits=18, decimal_places=4,
                validators=[django.core.validators.MinValueValidator(Decimal("0.0001"))],
            ),
        ),
        migrations.AddConstraint(
            model_name="purchaseinvoiceline",
            constraint=models.UniqueConstraint(
                fields=("invoice", "receipt_line"),
                name="uq_purchase_invoice_receipt_line",
            ),
        ),
        migrations.AddConstraint(
            model_name="purchaseinvoiceline",
            constraint=models.CheckConstraint(
                condition=models.Q(billed_quantity__gt=0),
                name="ck_purchase_invoice_line_qty_positive",
            ),
        ),
    ]
