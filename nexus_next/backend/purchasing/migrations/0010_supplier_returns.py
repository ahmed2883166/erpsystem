from django.db import migrations, models
import django.db.models.deletion
import django.core.validators
from decimal import Decimal


class Migration(migrations.Migration):
    dependencies = [
        ("accounting", "0001_initial"),
        ("accounts", "0001_initial"),
        ("companies", "0003_companymembership_branch_scope"),
        ("inventory", "0009_stock_cost_layer_precision"),
        ("purchasing", "0009_purchase_invoice_tax_allocation"),
        ("suppliers", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="SupplierReturn",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("return_number", models.CharField(max_length=40)),
                ("return_date", models.DateField()),
                ("status", models.CharField(choices=[("draft", "مسودة"), ("posted", "مرحّل")], default="draft", max_length=10)),
                ("net_amount", models.DecimalField(decimal_places=4, default=0, max_digits=20)),
                ("tax_amount", models.DecimalField(decimal_places=4, default=0, max_digits=20)),
                ("grand_total", models.DecimalField(decimal_places=4, default=0, max_digits=20)),
                ("notes", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("posted_at", models.DateTimeField(blank=True, null=True)),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="supplier_returns", to="companies.company")),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="created_supplier_returns", to="accounts.user")),
                ("invoice", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="returns", to="purchasing.purchaseinvoice")),
                ("journal_entry", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="supplier_return", to="accounting.journalentry")),
                ("stock_movement", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="supplier_return", to="inventory.stockmovement")),
                ("supplier", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="returns", to="suppliers.supplier")),
            ],
            options={"ordering": ("-return_date", "-id")},
        ),
        migrations.AddConstraint(
            model_name="supplierreturn",
            constraint=models.UniqueConstraint(fields=("company", "return_number"), name="uq_supplier_return_company_no"),
        ),
        migrations.AddIndex(
            model_name="supplierreturn",
            index=models.Index(fields=["company", "status", "return_date"], name="pur_return_co_status_date_idx"),
        ),
        migrations.CreateModel(
            name="SupplierReturnLine",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("quantity", models.DecimalField(decimal_places=4, max_digits=18, validators=[django.core.validators.MinValueValidator(Decimal("0.0001"))])),
                ("net_amount", models.DecimalField(decimal_places=4, default=0, max_digits=20)),
                ("tax_amount", models.DecimalField(decimal_places=4, default=0, max_digits=20)),
                ("recoverable_tax_amount", models.DecimalField(decimal_places=4, default=0, max_digits=20)),
                ("nonrecoverable_tax_amount", models.DecimalField(decimal_places=4, default=0, max_digits=20)),
                ("invoice_line", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="supplier_return_lines", to="purchasing.purchaseinvoiceline")),
                ("movement_line", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="supplier_return_line", to="inventory.stockmovementline")),
                ("return_document", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="lines", to="purchasing.supplierreturn")),
            ],
            options={"ordering": ("id",)},
        ),
        migrations.AddConstraint(
            model_name="supplierreturnline",
            constraint=models.UniqueConstraint(fields=("return_document", "invoice_line"), name="uq_supplier_return_invoice_line"),
        ),
    ]
