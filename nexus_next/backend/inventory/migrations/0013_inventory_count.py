import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0002_user_theme_preference"),
        ("companies", "0007_seed_default_currencies"),
        ("inventory", "0012_sales_return_reversal_line"),
    ]

    operations = [
        migrations.CreateModel(
            name="InventoryCount",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("count_number", models.CharField(max_length=40)),
                ("count_date", models.DateField()),
                ("status", models.CharField(choices=[("draft", "قيد العد"), ("posted", "مرحّل"), ("cancelled", "ملغي")], default="draft", max_length=10)),
                ("notes", models.TextField(blank=True)),
                ("posted_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="inventory_counts", to="companies.company")),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="created_inventory_counts", to=settings.AUTH_USER_MODEL)),
                ("gain_movement", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="gain_inventory_counts", to="inventory.stockmovement")),
                ("loss_movement", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="loss_inventory_counts", to="inventory.stockmovement")),
                ("posted_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="posted_inventory_counts", to=settings.AUTH_USER_MODEL)),
                ("warehouse", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="inventory_counts", to="inventory.warehouse")),
            ],
            options={
                "verbose_name": "مستند جرد مخزني",
                "verbose_name_plural": "مستندات الجرد المخزني",
                "ordering": ("-count_date", "-id"),
                "indexes": [models.Index(fields=["company", "status", "count_date"], name="inv_count_status_date_idx")],
                "constraints": [models.UniqueConstraint(fields=("company", "count_number"), name="uq_inventory_count_company_number")],
            },
        ),
        migrations.CreateModel(
            name="InventoryCountLine",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("expiry_date", models.DateField(blank=True, null=True)),
                ("system_quantity", models.DecimalField(decimal_places=4, default=0, max_digits=20)),
                ("system_value", models.DecimalField(decimal_places=4, default=0, max_digits=20)),
                ("counted_quantity", models.DecimalField(blank=True, decimal_places=4, max_digits=20, null=True)),
                ("adjustment_unit_cost", models.DecimalField(decimal_places=4, default=0, max_digits=20)),
                ("notes", models.TextField(blank=True)),
                ("counted_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("count", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="lines", to="inventory.inventorycount")),
                ("counted_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="counted_inventory_lines", to=settings.AUTH_USER_MODEL)),
                ("location", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="inventory_count_lines", to="inventory.storagelocation")),
                ("product", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="inventory_count_lines", to="inventory.product")),
            ],
            options={
                "verbose_name": "بند جرد مخزني",
                "verbose_name_plural": "بنود الجرد المخزني",
                "ordering": ("product__code", "id"),
                "indexes": [models.Index(fields=["count", "product"], name="inv_count_line_product_idx")],
                "constraints": [
                    models.UniqueConstraint(fields=("count", "product", "location", "expiry_date"), name="uq_inventory_count_line_dimension", nulls_distinct=False),
                    models.CheckConstraint(condition=models.Q(("system_quantity__gte", 0)), name="ck_inv_count_system_qty_nonnegative"),
                    models.CheckConstraint(condition=models.Q(("system_value__gte", 0)), name="ck_inv_count_system_value_nonnegative"),
                    models.CheckConstraint(condition=models.Q(("counted_quantity__isnull", True), ("counted_quantity__gte", 0), _connector="OR"), name="ck_inv_count_counted_qty_nonnegative"),
                    models.CheckConstraint(condition=models.Q(("adjustment_unit_cost__gte", 0)), name="ck_inv_count_unit_cost_nonnegative"),
                ],
            },
        ),
    ]
