import decimal
import django.core.validators
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("purchasing", "0014_supplier_refund_reversal"),
        ("companies", "0001_initial"),
        ("inventory", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="PurchaseRequisition",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("request_number", models.CharField(max_length=40)),
                ("request_date", models.DateField()),
                ("required_date", models.DateField(blank=True, null=True)),
                ("department", models.CharField(blank=True, max_length=120)),
                ("purpose", models.TextField(blank=True)),
                ("notes", models.TextField(blank=True)),
                ("status", models.CharField(choices=[("draft", "مسودة"), ("submitted", "مرسل للاعتماد"), ("approved", "معتمد"), ("rejected", "مرفوض"), ("converted", "تحول إلى أمر شراء"), ("cancelled", "ملغي")], default="draft", max_length=12)),
                ("reviewed_at", models.DateTimeField(blank=True, null=True)),
                ("review_note", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="purchase_requisitions", to="companies.company")),
                ("converted_order", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="source_requisition", to="purchasing.purchaseorder")),
                ("requested_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="purchase_requisitions", to=settings.AUTH_USER_MODEL)),
                ("reviewed_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="reviewed_purchase_requisitions", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ("-request_date", "-id")},
        ),
        migrations.CreateModel(
            name="PurchaseRequisitionLine",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("requested_quantity", models.DecimalField(decimal_places=4, max_digits=18, validators=[django.core.validators.MinValueValidator(decimal.Decimal('0.0001'))])),
                ("estimated_unit_price", models.DecimalField(blank=True, decimal_places=4, max_digits=18, null=True, validators=[django.core.validators.MinValueValidator(decimal.Decimal('0'))])),
                ("notes", models.CharField(blank=True, max_length=250)),
                ("product", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="purchase_requisition_lines", to="inventory.product")),
                ("requisition", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="lines", to="purchasing.purchaserequisition")),
            ],
            options={"ordering": ("id",)},
        ),
        migrations.AddConstraint(
            model_name="purchaserequisition",
            constraint=models.UniqueConstraint(fields=("company", "request_number"), name="uq_purchase_req_company_no"),
        ),
        migrations.AddIndex(
            model_name="purchaserequisition",
            index=models.Index(fields=["company", "status", "request_date"], name="pur_req_co_status_date_idx"),
        ),
    ]
