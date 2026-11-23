import django.core.validators
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("inventory", "0007_warehouse_product_setting")]

    operations = [
        migrations.AddField(
            model_name="stockcostlayer", name="root_source_line",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="root_cost_layers", to="inventory.stockmovementline"),
        ),
        migrations.AddField(
            model_name="stockcostlayer", name="parent_layer",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="transferred_layers", to="inventory.stockcostlayer"),
        ),
        migrations.CreateModel(
            name="StockCostConsumption",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("quantity", models.DecimalField(decimal_places=4, max_digits=20, validators=[django.core.validators.MinValueValidator(0.0001)])),
                ("unit_cost", models.DecimalField(decimal_places=6, max_digits=20, validators=[django.core.validators.MinValueValidator(0)])),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("movement_line", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="cost_consumptions", to="inventory.stockmovementline")),
                ("source_layer", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="consumptions", to="inventory.stockcostlayer")),
            ],
            options={"ordering": ("id",)},
        ),
        migrations.AddIndex(
            model_name="stockcostconsumption",
            index=models.Index(fields=["movement_line", "source_layer"], name="inv_cost_consume_idx"),
        ),
    ]
