import django.core.validators
import django.db.models.deletion
from decimal import Decimal
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0001_initial"),
        ("inventory", "0007_warehouse_product_setting"),
        ("purchasing", "0002_supplierpayment"),
        ("suppliers", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="PurchaseOrder",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("order_number", models.CharField(max_length=40)),
                ("order_date", models.DateField()),
                ("expected_date", models.DateField(blank=True, null=True)),
                ("status", models.CharField(choices=[("draft", "مسودة"), ("confirmed", "معتمد"), ("partially_received", "استلام جزئي"), ("received", "مستلم بالكامل"), ("cancelled", "ملغي")], default="draft", max_length=20)),
                ("notes", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("confirmed_at", models.DateTimeField(blank=True, null=True)),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="purchase_orders", to="companies.company")),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="created_purchase_orders", to="accounts.user")),
                ("supplier", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="purchase_orders", to="suppliers.supplier")),
                ("warehouse", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="purchase_orders", to="inventory.warehouse")),
            ],
            options={"ordering": ("-order_date", "-id"), "indexes": [models.Index(fields=["company", "status", "order_date"], name="pur_order_co_status_date_idx")], "constraints": [models.UniqueConstraint(fields=("company", "order_number"), name="uq_purchase_order_company_no")]},
        ),
        migrations.CreateModel(
            name="PurchaseOrderLine",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("quantity", models.DecimalField(decimal_places=4, max_digits=18, validators=[django.core.validators.MinValueValidator(Decimal("0.0001"))])),
                ("unit_price", models.DecimalField(decimal_places=4, max_digits=18, validators=[django.core.validators.MinValueValidator(Decimal("0"))])),
                ("tax_rate", models.DecimalField(decimal_places=4, default=0, max_digits=7, validators=[django.core.validators.MinValueValidator(0), django.core.validators.MaxValueValidator(100)])),
                ("received_quantity", models.DecimalField(decimal_places=4, default=0, max_digits=18)),
                ("order", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="lines", to="purchasing.purchaseorder")),
                ("product", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="purchase_order_lines", to="inventory.product")),
            ],
            options={"ordering": ("id",)},
        ),
        migrations.CreateModel(
            name="PurchaseOrderReceiptLine",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("quantity", models.DecimalField(decimal_places=4, max_digits=18, validators=[django.core.validators.MinValueValidator(Decimal("0.0001"))])),
                ("order_line", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="receipt_links", to="purchasing.purchaseorderline")),
                ("receipt_line", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="purchase_order_link", to="inventory.stockmovementline")),
            ],
            options={"ordering": ("id",)},
        ),
        migrations.AddField(
            model_name="purchasereceipt",
            name="purchase_order",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="receipts", to="purchasing.purchaseorder"),
        ),
    ]
