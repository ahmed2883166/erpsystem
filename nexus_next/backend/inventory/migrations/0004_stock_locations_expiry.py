import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("inventory", "0003_fifo_cost_layers")]

    operations = [
        migrations.AddField(
            model_name="stockmovementline", name="source_location",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="source_stock_lines", to="inventory.storagelocation"),
        ),
        migrations.AddField(
            model_name="stockmovementline", name="destination_location",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="destination_stock_lines", to="inventory.storagelocation"),
        ),
        migrations.AddField(
            model_name="stockmovementline", name="expiry_date",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="stockbalance", name="location",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="stock_balances", to="inventory.storagelocation"),
        ),
        migrations.AddField(
            model_name="stockbalance", name="expiry_date",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.RemoveConstraint(
            model_name="stockbalance", name="uq_stock_balance_product_warehouse",
        ),
        migrations.AddConstraint(
            model_name="stockbalance",
            constraint=models.UniqueConstraint(fields=("product", "warehouse", "location", "expiry_date"), name="uq_stock_balance_product_warehouse_location_expiry", nulls_distinct=False),
        ),
        migrations.AddField(
            model_name="stockcostlayer", name="location",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="fifo_cost_layers", to="inventory.storagelocation"),
        ),
        migrations.AddField(
            model_name="stockcostlayer", name="expiry_date",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.RemoveIndex(
            model_name="stockcostlayer", name="inv_fifo_layer_order_idx",
        ),
        migrations.AddIndex(
            model_name="stockcostlayer",
            index=models.Index(fields=["product", "warehouse", "location", "created_at"], name="inv_fifo_layer_order_idx"),
        ),
    ]
