import django.core.validators
import django.db.models.deletion
from django.db import migrations, models
import decimal


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0001_initial"),
        ("accounting", "0006_payroll_tax_payable_role"),
        ("companies", "0001_initial"),
        ("inventory", "0007_warehouse_product_setting"),
        ("sales", "0005_customer_receipt_account"),
    ]

    operations = [
        migrations.CreateModel(
            name="SalesReturn",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("return_number", models.CharField(max_length=40)),
                ("return_date", models.DateField()),
                ("status", models.CharField(choices=[("draft", "مسودة"), ("posted", "مرحّل")], default="draft", max_length=10)),
                ("net_total", models.DecimalField(decimal_places=4, default=0, max_digits=20)),
                ("tax_total", models.DecimalField(decimal_places=4, default=0, max_digits=20)),
                ("grand_total", models.DecimalField(decimal_places=4, default=0, max_digits=20)),
                ("notes", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("posted_at", models.DateTimeField(blank=True, null=True)),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="sales_returns", to="companies.company")),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="created_sales_returns", to="accounts.user")),
                ("invoice", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="returns", to="sales.salesinvoice")),
                ("warehouse", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="sales_returns", to="inventory.warehouse")),
                ("stock_movement", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="sales_return", to="inventory.stockmovement")),
                ("journal_entry", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="sales_return", to="accounting.journalentry")),
                ("cost_journal_entry", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="sales_return_cost", to="accounting.journalentry")),
            ],
            options={"ordering": ("-return_date", "-id")},
        ),
        migrations.AddConstraint(
            model_name="salesreturn",
            constraint=models.UniqueConstraint(fields=("company", "return_number"), name="uq_sales_return_company_no"),
        ),
        migrations.AddIndex(
            model_name="salesreturn",
            index=models.Index(fields=["company", "status", "return_date"], name="sales_return_co_status_idx"),
        ),
        migrations.CreateModel(
            name="SalesReturnLine",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("quantity", models.DecimalField(decimal_places=4, max_digits=18, validators=[django.core.validators.MinValueValidator(decimal.Decimal("0.0001"))])),
                ("net_amount", models.DecimalField(decimal_places=4, default=0, max_digits=20)),
                ("tax_amount", models.DecimalField(decimal_places=4, default=0, max_digits=20)),
                ("total_amount", models.DecimalField(decimal_places=4, default=0, max_digits=20)),
                ("cost_amount", models.DecimalField(decimal_places=4, default=0, max_digits=20)),
                ("invoice_line", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="return_lines", to="sales.salesinvoiceline")),
                ("movement_line", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="sales_return_line", to="inventory.stockmovementline")),
                ("sales_return", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="lines", to="sales.salesreturn")),
            ],
            options={"ordering": ("id",)},
        ),
        migrations.AddConstraint(
            model_name="salesreturnline",
            constraint=models.UniqueConstraint(fields=("sales_return", "invoice_line"), name="uq_sales_return_invoice_line"),
        ),
    ]
