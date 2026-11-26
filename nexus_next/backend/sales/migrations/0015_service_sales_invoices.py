import django.core.validators
import django.db.models.deletion
from decimal import Decimal
from django.db import migrations, models


def backfill_invoice_line_product_and_quantity(apps, schema_editor):
    InvoiceLine = apps.get_model("sales", "SalesInvoiceLine")
    for line in InvoiceLine.objects.filter(delivery_line__isnull=False).select_related("delivery_line"):
        source = line.delivery_line
        InvoiceLine.objects.filter(pk=line.pk).update(
            product_id=source.product_id,
            quantity=source.quantity,
        )


class Migration(migrations.Migration):
    dependencies = [
        ("sales", "0014_sales_document_templates"),
        ("inventory", "0018_stock_line_product_movement_index"),
        ("companies", "0001_initial"),
    ]

    operations = [
        migrations.AlterField(
            model_name="salesinvoice",
            name="delivery",
            field=models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="invoice", to="sales.salesdelivery"),
        ),
        migrations.AddField(
            model_name="salesinvoice",
            name="branch",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="sales_invoices", to="companies.branch"),
        ),
        migrations.AddField(
            model_name="salesinvoiceline",
            name="product",
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.PROTECT, related_name="sales_invoice_lines", to="inventory.product"),
        ),
        migrations.AddField(
            model_name="salesinvoiceline",
            name="quantity",
            field=models.DecimalField(decimal_places=4, default=1, max_digits=18, validators=[django.core.validators.MinValueValidator(Decimal("0.0001"))]),
        ),
        migrations.RunPython(backfill_invoice_line_product_and_quantity, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="salesinvoiceline",
            name="product",
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="sales_invoice_lines", to="inventory.product"),
        ),
        migrations.AlterField(
            model_name="salesinvoiceline",
            name="delivery_line",
            field=models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="sales_invoice_line", to="inventory.stockmovementline"),
        ),
    ]
