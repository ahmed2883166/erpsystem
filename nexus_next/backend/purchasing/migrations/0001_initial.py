import django.core.validators
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True
    dependencies = [
        ("accounts", "0001_initial"),
        ("accounting", "0003_inventory_account_roles"),
        ("companies", "0003_companymembership_branch_scope"),
        ("inventory", "0007_warehouse_product_setting"),
        ("suppliers", "0001_initial"),
    ]
    operations = [
        migrations.CreateModel(
            name="PurchaseReceipt",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("receipt_number", models.CharField(max_length=40)),
                ("receipt_date", models.DateField()),
                ("supplier_delivery_ref", models.CharField(blank=True, max_length=100)),
                ("notes", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="purchase_receipts", to="companies.company")),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="created_purchase_receipts", to="accounts.user")),
                ("stock_movement", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="purchase_receipt", to="inventory.stockmovement")),
                ("supplier", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="purchase_receipts", to="suppliers.supplier")),
                ("warehouse", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="purchase_receipts", to="inventory.warehouse")),
            ],
            options={"ordering": ("-receipt_date", "-id"), "indexes": [models.Index(fields=["company", "receipt_date"], name="pur_receipt_co_date_idx")], "constraints": [models.UniqueConstraint(fields=("company", "receipt_number"), name="uq_purchase_receipt_company_no")]},
        ),
        migrations.CreateModel(
            name="PurchaseInvoice",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("invoice_number", models.CharField(max_length=80)),
                ("invoice_date", models.DateField()),
                ("status", models.CharField(choices=[("draft", "مسودة"), ("posted", "مرحّلة")], default="draft", max_length=10)),
                ("net_total", models.DecimalField(decimal_places=4, default=0, max_digits=20)),
                ("tax_total", models.DecimalField(decimal_places=4, default=0, max_digits=20)),
                ("grand_total", models.DecimalField(decimal_places=4, default=0, max_digits=20)),
                ("notes", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("posted_at", models.DateTimeField(blank=True, null=True)),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="purchase_invoices", to="companies.company")),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="created_purchase_invoices", to="accounts.user")),
                ("journal_entry", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="purchase_invoice", to="accounting.journalentry")),
                ("receipt", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="invoice", to="purchasing.purchasereceipt")),
                ("supplier", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="purchase_invoices", to="suppliers.supplier")),
            ],
            options={"ordering": ("-invoice_date", "-id"), "indexes": [models.Index(fields=["company", "status", "invoice_date"], name="pur_invoice_co_status_date_idx")], "constraints": [models.UniqueConstraint(fields=("company", "invoice_number"), name="uq_purchase_invoice_company_no")]},
        ),
        migrations.CreateModel(
            name="PurchaseInvoiceLine",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("unit_price", models.DecimalField(decimal_places=4, max_digits=18, validators=[django.core.validators.MinValueValidator(0)])),
                ("tax_rate", models.DecimalField(decimal_places=4, default=0, max_digits=7, validators=[django.core.validators.MinValueValidator(0), django.core.validators.MaxValueValidator(100)])),
                ("net_amount", models.DecimalField(decimal_places=4, default=0, max_digits=20)),
                ("tax_amount", models.DecimalField(decimal_places=4, default=0, max_digits=20)),
                ("total_amount", models.DecimalField(decimal_places=4, default=0, max_digits=20)),
                ("invoice", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="lines", to="purchasing.purchaseinvoice")),
                ("receipt_line", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="purchase_invoice_line", to="inventory.stockmovementline")),
            ],
            options={"ordering": ("id",)},
        ),
    ]
