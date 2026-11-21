import django.core.validators
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="StockMovement",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("movement_number", models.CharField(max_length=40)),
                ("movement_type", models.CharField(choices=[("receipt", "استلام"), ("issue", "صرف"), ("transfer", "تحويل"), ("adjustment", "تسوية جرد")], max_length=12)),
                ("movement_date", models.DateField()),
                ("status", models.CharField(choices=[("draft", "مسودة"), ("posted", "مرحّل")], default="draft", max_length=10)),
                ("notes", models.TextField(blank=True)),
                ("posted_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="stock_movements", to="companies.company")),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="created_stock_movements", to=settings.AUTH_USER_MODEL)),
                ("destination_warehouse", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="incoming_movements", to="inventory.warehouse")),
                ("posted_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="posted_stock_movements", to=settings.AUTH_USER_MODEL)),
                ("source_warehouse", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="outgoing_movements", to="inventory.warehouse")),
            ],
            options={"ordering": ("-movement_date", "-id"), "constraints": [models.UniqueConstraint(fields=("company", "movement_number"), name="uq_stock_movement_company_number")], "indexes": [models.Index(fields=["company", "status", "movement_date"], name="inv_move_status_date_idx")]},
        ),
        migrations.CreateModel(
            name="StockMovementLine",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("quantity", models.DecimalField(decimal_places=4, max_digits=18, validators=[django.core.validators.MinValueValidator(0.0001)])),
                ("unit_cost", models.DecimalField(decimal_places=4, default=0, max_digits=18, validators=[django.core.validators.MinValueValidator(0)])),
                ("adjustment_direction", models.CharField(blank=True, choices=[("in", "زيادة"), ("out", "عجز")], max_length=3)),
                ("movement", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="lines", to="inventory.stockmovement")),
                ("product", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="stock_movement_lines", to="inventory.product")),
            ],
            options={"ordering": ("id",)},
        ),
        migrations.CreateModel(
            name="StockBalance",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("quantity", models.DecimalField(decimal_places=4, default=0, max_digits=20)),
                ("inventory_value", models.DecimalField(decimal_places=4, default=0, max_digits=20)),
                ("average_cost", models.DecimalField(decimal_places=6, default=0, max_digits=20)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("product", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="stock_balances", to="inventory.product")),
                ("warehouse", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="stock_balances", to="inventory.warehouse")),
            ],
            options={"constraints": [models.UniqueConstraint(fields=("product", "warehouse"), name="uq_stock_balance_product_warehouse"), models.CheckConstraint(condition=models.Q(("quantity__gte", 0)), name="ck_stock_balance_nonnegative"), models.CheckConstraint(condition=models.Q(("inventory_value__gte", 0)), name="ck_stock_balance_value_nonnegative")], "indexes": [models.Index(fields=["warehouse", "product"], name="inv_balance_wh_product_idx")]},
        ),
    ]
