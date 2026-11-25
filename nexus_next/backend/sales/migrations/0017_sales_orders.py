import django.core.validators
import django.db.models.deletion
from django.db import migrations, models
import decimal


class Migration(migrations.Migration):
    dependencies = [
        ("sales", "0016_service_sales_returns"),
    ]

    operations = [
        migrations.CreateModel(
            name="SalesOrder",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("order_number", models.CharField(max_length=80)),
                ("order_date", models.DateField()),
                ("requested_delivery_date", models.DateField(blank=True, null=True)),
                ("status", models.CharField(choices=[("draft", "مسودة"), ("confirmed", "معتمد"), ("partially_delivered", "تم التسليم جزئيًا"), ("delivered", "تم التسليم"), ("cancelled", "ملغي")], default="draft", max_length=24)),
                ("net_total", models.DecimalField(decimal_places=4, default=0, max_digits=20)),
                ("discount_total", models.DecimalField(decimal_places=4, default=0, max_digits=20)),
                ("tax_total", models.DecimalField(decimal_places=4, default=0, max_digits=20)),
                ("grand_total", models.DecimalField(decimal_places=4, default=0, max_digits=20)),
                ("notes", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="sales_orders", to="companies.company")),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="created_sales_orders", to="accounts.user")),
                ("customer", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="sales_orders", to="customers.customer")),
                ("warehouse", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="sales_orders", to="inventory.warehouse")),
            ],
            options={
                "ordering": ("-order_date", "-id"),
                "indexes": [models.Index(fields=["company", "status", "order_date"], name="sales_order_co_status_idx")],
                "constraints": [
                    models.UniqueConstraint(fields=("company", "order_number"), name="uq_sales_order_company_no"),
                    models.CheckConstraint(condition=models.Q(requested_delivery_date__isnull=True) | models.Q(requested_delivery_date__gte=models.F("order_date")), name="ck_sales_order_delivery_date"),
                ],
            },
        ),
        migrations.CreateModel(
            name="SalesOrderLine",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("quantity", models.DecimalField(decimal_places=4, max_digits=18, validators=[django.core.validators.MinValueValidator(decimal.Decimal("0.0001"))])),
                ("unit_price", models.DecimalField(decimal_places=4, max_digits=18, validators=[django.core.validators.MinValueValidator(0)])),
                ("tax_rate", models.DecimalField(decimal_places=4, default=0, max_digits=7, validators=[django.core.validators.MinValueValidator(0), django.core.validators.MaxValueValidator(100)])),
                ("discount_percent", models.DecimalField(decimal_places=4, default=0, max_digits=7, validators=[django.core.validators.MinValueValidator(0), django.core.validators.MaxValueValidator(100)])),
                ("discount_amount", models.DecimalField(decimal_places=4, default=0, max_digits=20)),
                ("net_amount", models.DecimalField(decimal_places=4, default=0, max_digits=20)),
                ("tax_amount", models.DecimalField(decimal_places=4, default=0, max_digits=20)),
                ("total_amount", models.DecimalField(decimal_places=4, default=0, max_digits=20)),
                ("order", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="lines", to="sales.salesorder")),
                ("product", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="sales_order_lines", to="inventory.product")),
                ("tax_code", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="sales_order_lines", to="accounting.taxcode")),
            ],
            options={"ordering": ("id",)},
        ),
    ]
