import django.core.validators
import django.db.models.deletion
from decimal import Decimal
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("accounting", "0001_initial"),
        ("accounts", "0001_initial"),
        ("purchasing", "0012_supplier_return_reversal"),
        ("suppliers", "0001_initial"),
        ("companies", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="SupplierRefund",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("refund_number", models.CharField(max_length=40)),
                ("refund_date", models.DateField()),
                ("method", models.CharField(choices=[("cash", "نقدي"), ("bank", "تحويل بنكي")], default="bank", max_length=8)),
                ("amount", models.DecimalField(decimal_places=4, max_digits=20, validators=[django.core.validators.MinValueValidator(Decimal("0.0001"))])),
                ("reference", models.CharField(blank=True, max_length=120)),
                ("notes", models.TextField(blank=True)),
                ("status", models.CharField(choices=[("draft", "مسودة"), ("posted", "مرحّل")], default="draft", max_length=10)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("posted_at", models.DateTimeField(blank=True, null=True)),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="supplier_refunds", to="companies.company")),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="created_supplier_refunds", to="accounts.user")),
                ("journal_entry", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="supplier_refund", to="accounting.journalentry")),
                ("payment_account", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="supplier_refunds", to="accounting.chartofaccount")),
                ("return_document", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="refunds", to="purchasing.supplierreturn")),
                ("supplier", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="refunds", to="suppliers.supplier")),
            ],
            options={
                "ordering": ("-refund_date", "-id"),
                "indexes": [models.Index(fields=["company", "status", "refund_date"], name="pur_refund_co_status_idx")],
                "constraints": [models.UniqueConstraint(fields=("company", "refund_number"), name="uq_supplier_refund_company_no")],
            },
        ),
    ]
