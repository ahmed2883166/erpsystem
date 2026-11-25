import django.core.validators
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("inventory", "0002_stock_movements")]

    operations = [
        migrations.CreateModel(
            name="StockCostLayer",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("quantity_remaining", models.DecimalField(decimal_places=4, max_digits=20, validators=[django.core.validators.MinValueValidator(0)])),
                ("unit_cost", models.DecimalField(decimal_places=6, max_digits=20, validators=[django.core.validators.MinValueValidator(0)])),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("product", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="cost_layers", to="inventory.product")),
                ("source_line", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="created_cost_layers", to="inventory.stockmovementline")),
                ("warehouse", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="cost_layers", to="inventory.warehouse")),
            ],
            options={"ordering": ("created_at", "id"), "indexes": [models.Index(fields=["product", "warehouse", "created_at"], name="inv_fifo_layer_order_idx")], "constraints": [models.CheckConstraint(condition=models.Q(("quantity_remaining__gte", 0)), name="ck_fifo_layer_nonnegative")]},
        ),
    ]
