import django.core.validators
import django.db.models.deletion
from decimal import Decimal
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("accounts", "0001_initial"), ("accounting", "0003_inventory_account_roles"), ("companies", "0003_companymembership_branch_scope"), ("purchasing", "0001_initial"), ("suppliers", "0001_initial")]
    operations = [
        migrations.CreateModel(
            name="SupplierPayment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("payment_number", models.CharField(max_length=40)),
                ("payment_date", models.DateField()),
                ("method", models.CharField(choices=[("cash", "نقدي"), ("bank", "تحويل بنكي")], default="bank", max_length=8)),
                ("amount", models.DecimalField(decimal_places=4, max_digits=20, validators=[django.core.validators.MinValueValidator(Decimal("0.0001"))])),
                ("reference", models.CharField(blank=True, max_length=120)),
                ("status", models.CharField(choices=[("draft", "مسودة"), ("posted", "مرحّل")], default="draft", max_length=10)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("posted_at", models.DateTimeField(blank=True, null=True)),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="supplier_payments", to="companies.company")),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="created_supplier_payments", to="accounts.user")),
                ("invoice", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="payments", to="purchasing.purchaseinvoice")),
                ("journal_entry", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="supplier_payment", to="accounting.journalentry")),
                ("supplier", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="payments", to="suppliers.supplier")),
            ],
            options={
                "ordering": ("-payment_date", "-id"),
                "indexes": [models.Index(fields=["company", "status", "payment_date"], name="pur_payment_co_status_idx")],
                "constraints": [models.UniqueConstraint(fields=("company", "payment_number"), name="uq_supplier_payment_company_no")],
            },
        ),
    ]
