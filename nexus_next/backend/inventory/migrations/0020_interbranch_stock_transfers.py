from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
from django.db.models import Q
import django.core.validators


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0019_stock_card_order_index"),
    ]

    operations = [
        migrations.AddField(
            model_name="warehouse",
            name="is_transit",
            field=models.BooleanField(default=False, editable=False),
        ),
        migrations.AddConstraint(
            model_name="warehouse",
            constraint=models.UniqueConstraint(
                condition=Q(is_transit=True),
                fields=("company",),
                name="uq_warehouse_company_transit",
            ),
        ),
        migrations.CreateModel(
            name="StockTransfer",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("transfer_number", models.CharField(max_length=40)),
                ("transfer_date", models.DateField()),
                ("status", models.CharField(choices=[("draft", "مسودة"), ("in_transit", "بضاعة بالطريق"), ("partially_received", "استلام جزئي"), ("received", "مكتمل الاستلام"), ("cancelled", "ملغي")], default="draft", max_length=20)),
                ("notes", models.TextField(blank=True)),
                ("dispatched_at", models.DateTimeField(blank=True, null=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="stock_transfers", to="companies.company")),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="created_stock_transfers", to=settings.AUTH_USER_MODEL)),
                ("dispatched_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="dispatched_stock_transfers", to=settings.AUTH_USER_MODEL)),
                ("destination_warehouse", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="stock_transfers_in", to="inventory.warehouse")),
                ("dispatch_movement", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="dispatched_stock_transfer", to="inventory.stockmovement")),
                ("source_warehouse", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="stock_transfers_out", to="inventory.warehouse")),
            ],
            options={"ordering": ("-transfer_date", "-id")},
        ),
        migrations.AddConstraint(
            model_name="stocktransfer",
            constraint=models.UniqueConstraint(fields=("company", "transfer_number"), name="uq_stock_transfer_company_number"),
        ),
        migrations.AddIndex(
            model_name="stocktransfer",
            index=models.Index(fields=["company", "status", "transfer_date"], name="inv_transfer_status_date_idx"),
        ),
        migrations.CreateModel(
            name="StockTransferLine",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("expiry_date", models.DateField(blank=True, null=True)),
                ("quantity", models.DecimalField(decimal_places=4, max_digits=18, validators=[django.core.validators.MinValueValidator(0.0001)])),
                ("received_quantity", models.DecimalField(decimal_places=4, default=0, max_digits=18)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("product", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="stock_transfer_lines", to="inventory.product")),
                ("transfer", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="lines", to="inventory.stocktransfer")),
            ],
            options={"ordering": ("product__code", "id")},
        ),
        migrations.AddConstraint(
            model_name="stocktransferline",
            constraint=models.UniqueConstraint(fields=("transfer", "product", "expiry_date"), name="uq_stock_transfer_line_batch", nulls_distinct=False),
        ),
        migrations.AddConstraint(
            model_name="stocktransferline",
            constraint=models.CheckConstraint(condition=Q(("quantity__gt", 0)), name="ck_stock_transfer_qty_positive"),
        ),
        migrations.AddConstraint(
            model_name="stocktransferline",
            constraint=models.CheckConstraint(condition=Q(("received_quantity__gte", 0), ("received_quantity__lte", models.F("quantity"))), name="ck_stock_transfer_received_range"),
        ),
        migrations.CreateModel(
            name="StockTransferReceipt",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("receipt_number", models.CharField(max_length=40)),
                ("receipt_date", models.DateField()),
                ("received_at", models.DateTimeField(auto_now_add=True)),
                ("movement", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="stock_transfer_receipt", to="inventory.stockmovement")),
                ("received_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="stock_transfer_receipts", to=settings.AUTH_USER_MODEL)),
                ("transfer", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="receipts", to="inventory.stocktransfer")),
            ],
            options={"ordering": ("receipt_date", "id")},
        ),
        migrations.AddConstraint(
            model_name="stocktransferreceipt",
            constraint=models.UniqueConstraint(fields=("transfer", "receipt_number"), name="uq_stock_transfer_receipt_number"),
        ),
        migrations.CreateModel(
            name="StockTransferReceiptLine",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("quantity", models.DecimalField(decimal_places=4, max_digits=18, validators=[django.core.validators.MinValueValidator(0.0001)])),
                ("movement_line", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="stock_transfer_receipt_line", to="inventory.stockmovementline")),
                ("receipt", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="lines", to="inventory.stocktransferreceipt")),
                ("transfer_line", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="receipt_lines", to="inventory.stocktransferline")),
            ],
            options={"ordering": ("id",)},
        ),
        migrations.AddConstraint(
            model_name="stocktransferreceiptline",
            constraint=models.UniqueConstraint(fields=("receipt", "transfer_line"), name="uq_transfer_receipt_line"),
        ),
        migrations.AddConstraint(
            model_name="stocktransferreceiptline",
            constraint=models.CheckConstraint(condition=Q(("quantity__gt", 0)), name="ck_transfer_receipt_qty_positive"),
        ),
    ]
