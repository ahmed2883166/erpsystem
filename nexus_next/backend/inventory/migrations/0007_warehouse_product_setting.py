import django.core.validators
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0006_stock_line_cost_amount"),
    ]

    operations = [
        migrations.CreateModel(
            name="WarehouseProductSetting",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("min_stock", models.DecimalField(decimal_places=4, default=0, max_digits=18, validators=[django.core.validators.MinValueValidator(0)])),
                ("is_active", models.BooleanField(default=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("product", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="warehouse_settings", to="inventory.product")),
                ("warehouse", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="product_settings", to="inventory.warehouse")),
            ],
            options={
                "verbose_name": "حد إعادة طلب الصنف بالمخزن",
                "verbose_name_plural": "حدود إعادة الطلب بالمخازن",
                "ordering": ("warehouse__code", "product__code"),
            },
        ),
        migrations.AddConstraint(
            model_name="warehouseproductsetting",
            constraint=models.UniqueConstraint(fields=("product", "warehouse"), name="uq_wh_product_setting"),
        ),
        migrations.AddIndex(
            model_name="warehouseproductsetting",
            index=models.Index(fields=["warehouse", "is_active"], name="inv_wh_setting_active_idx"),
        ),
    ]
