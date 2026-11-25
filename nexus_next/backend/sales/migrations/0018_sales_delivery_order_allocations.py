import django.core.validators
import django.db.models.deletion
from django.db import migrations, models
import decimal


class Migration(migrations.Migration):
    dependencies = [
        ("sales", "0017_sales_orders"),
        ("inventory", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="salesdelivery",
            name="order",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="deliveries", to="sales.salesorder"),
        ),
        migrations.CreateModel(
            name="SalesDeliveryAllocation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("quantity", models.DecimalField(decimal_places=4, max_digits=18, validators=[django.core.validators.MinValueValidator(decimal.Decimal("0.0001"))])),
                ("delivery", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="order_allocations", to="sales.salesdelivery")),
                ("movement_line", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="sales_order_allocation", to="inventory.stockmovementline")),
                ("order_line", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="delivery_allocations", to="sales.salesorderline")),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(fields=("delivery", "order_line"), name="uq_delivery_order_line"),
                ],
            },
        ),
    ]
