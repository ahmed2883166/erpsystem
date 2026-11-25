from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0015_product_stock_class"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="inventorycount",
            constraint=models.UniqueConstraint(
                condition=models.Q(("status", "draft")),
                fields=("warehouse",),
                name="uq_inventory_count_open_warehouse",
            ),
        ),
    ]
