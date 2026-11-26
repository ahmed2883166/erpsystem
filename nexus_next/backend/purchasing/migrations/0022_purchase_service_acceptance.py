import django.core.validators
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from decimal import Decimal


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("companies", "0008_company_multi_activity"),
        ("purchasing", "0021_purchase_invoice_activity"),
        ("suppliers", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="PurchaseServiceAcceptance",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("acceptance_number", models.CharField(max_length=40)),
                ("acceptance_date", models.DateField()),
                ("status", models.CharField(choices=[("draft", "مسودة"), ("accepted", "معتمد"), ("rejected", "مرفوض")], default="draft", max_length=12)),
                ("notes", models.TextField(blank=True)),
                ("accepted_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("accepted_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="accepted_purchase_services", to=settings.AUTH_USER_MODEL)),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="purchase_service_acceptances", to="companies.company")),
                ("order", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="service_acceptances", to="purchasing.purchaseorder")),
                ("supplier", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="service_acceptances", to="suppliers.supplier")),
            ],
            options={
                "ordering": ("-acceptance_date", "-id"),
                "indexes": [models.Index(fields=["company", "status", "acceptance_date"], name="pur_svc_accept_co_date_idx")],
                "constraints": [models.UniqueConstraint(fields=("company", "acceptance_number"), name="uq_purchase_service_acceptance_no")],
            },
        ),
        migrations.CreateModel(
            name="PurchaseServiceAcceptanceLine",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("quantity", models.DecimalField(decimal_places=4, max_digits=18, validators=[django.core.validators.MinValueValidator(Decimal("0.0001"))])),
                ("notes", models.CharField(blank=True, max_length=250)),
                ("acceptance", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="lines", to="purchasing.purchaseserviceacceptance")),
                ("order_line", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="service_acceptance_lines", to="purchasing.purchaseorderline")),
            ],
            options={
                "ordering": ("id",),
                "constraints": [models.UniqueConstraint(fields=("acceptance", "order_line"), name="uq_purchase_service_accept_line")],
            },
        ),
    ]
