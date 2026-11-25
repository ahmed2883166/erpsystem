from decimal import Decimal
import django.core.validators
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True
    dependencies = [
        ("accounts", "0001_initial"),
        ("accounting", "0003_inventory_account_roles"),
        ("companies", "0003_companymembership_branch_scope"),
        ("customers", "0001_initial"),
        ("inventory", "0007_warehouse_product_setting"),
    ]
    operations = [
        migrations.CreateModel(
            name="SalesDelivery",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("delivery_number", models.CharField(max_length=40)),
                ("delivery_date", models.DateField()),
                ("customer_reference", models.CharField(blank=True, max_length=100)),
                ("notes", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="sales_deliveries", to="companies.company")),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="created_sales_deliveries", to="accounts.user")),
                ("customer", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="sales_deliveries", to="customers.customer")),
                ("stock_movement", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="sales_delivery", to="inventory.stockmovement")),
                ("warehouse", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="sales_deliveries", to="inventory.warehouse")),
            ],
            options={"ordering": ("-delivery_date", "-id"), "indexes": [models.Index(fields=["company", "delivery_date"], name="sales_delivery_co_date_idx")], "constraints": [models.UniqueConstraint(fields=("company", "delivery_number"), name="uq_sales_delivery_company_no")]},
        ),
        migrations.CreateModel(
            name="SalesInvoice",
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
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="sales_invoices", to="companies.company")),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="created_sales_invoices", to="accounts.user")),
                ("customer", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="sales_invoices", to="customers.customer")),
                ("delivery", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="invoice", to="sales.salesdelivery")),
                ("journal_entry", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="sales_invoice", to="accounting.journalentry")),
            ],
            options={"ordering": ("-invoice_date", "-id"), "indexes": [models.Index(fields=["company", "status", "invoice_date"], name="sales_invoice_co_status_idx")], "constraints": [models.UniqueConstraint(fields=("company", "invoice_number"), name="uq_sales_invoice_company_no")]},
        ),
        migrations.CreateModel(
            name="CustomerReceipt",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("receipt_number", models.CharField(max_length=40)),
                ("receipt_date", models.DateField()),
                ("method", models.CharField(choices=[("cash", "نقدي"), ("bank", "تحويل بنكي")], default="bank", max_length=8)),
                ("amount", models.DecimalField(decimal_places=4, max_digits=20, validators=[django.core.validators.MinValueValidator(Decimal("0.0001"))])),
                ("reference", models.CharField(blank=True, max_length=120)),
                ("status", models.CharField(choices=[("draft", "مسودة"), ("posted", "مرحّل")], default="draft", max_length=10)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("posted_at", models.DateTimeField(blank=True, null=True)),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="customer_receipts", to="companies.company")),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="created_customer_receipts", to="accounts.user")),
                ("customer", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="receipts", to="customers.customer")),
                ("invoice", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="receipts", to="sales.salesinvoice")),
                ("journal_entry", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="customer_receipt", to="accounting.journalentry")),
            ],
            options={"ordering": ("-receipt_date", "-id"), "indexes": [models.Index(fields=["company", "status", "receipt_date"], name="sales_receipt_co_status_idx")], "constraints": [models.UniqueConstraint(fields=("company", "receipt_number"), name="uq_customer_receipt_company_no")]},
        ),
        migrations.CreateModel(
            name="SalesInvoiceLine",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("unit_price", models.DecimalField(decimal_places=4, max_digits=18, validators=[django.core.validators.MinValueValidator(0)])),
                ("tax_rate", models.DecimalField(decimal_places=4, default=0, max_digits=7, validators=[django.core.validators.MinValueValidator(0), django.core.validators.MaxValueValidator(100)])),
                ("net_amount", models.DecimalField(decimal_places=4, default=0, max_digits=20)),
                ("tax_amount", models.DecimalField(decimal_places=4, default=0, max_digits=20)),
                ("total_amount", models.DecimalField(decimal_places=4, default=0, max_digits=20)),
                ("delivery_line", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="sales_invoice_line", to="inventory.stockmovementline")),
                ("invoice", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="lines", to="sales.salesinvoice")),
            ],
            options={"ordering": ("id",)},
        ),
    ]
