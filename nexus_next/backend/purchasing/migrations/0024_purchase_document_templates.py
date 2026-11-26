import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("companies", "0001_initial"),
        ("purchasing", "0023_purchase_invoice_service_acceptance"),
    ]

    operations = [
        migrations.CreateModel(
            name="PurchaseDocumentTemplate",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("document_type", models.CharField(
                    choices=[
                        ("purchase_order", "أمر شراء"),
                        ("purchase_receipt", "إذن استلام"),
                        ("purchase_invoice", "فاتورة مورد"),
                        ("supplier_return", "مرتجع مورد"),
                        ("supplier_payment", "سند صرف"),
                        ("supplier_refund", "استرداد مورد"),
                    ],
                    max_length=24,
                )),
                ("name", models.CharField(max_length=80)),
                ("style", models.CharField(
                    choices=[("classic", "Classic"), ("modern", "Modern"), ("compact", "Compact")],
                    default="classic",
                    max_length=12,
                )),
                ("config", models.JSONField(blank=True, default=dict)),
                ("is_default", models.BooleanField(default=False)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("company", models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="purchase_document_templates",
                    to="companies.company",
                )),
            ],
            options={
                "ordering": ("document_type", "-is_default", "name"),
                "indexes": [
                    models.Index(fields=["company", "document_type", "is_active"], name="purchase_template_co_type_idx"),
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("company", "document_type", "name"),
                        name="uq_purchase_template_company_type_name",
                    ),
                    models.UniqueConstraint(
                        condition=models.Q(("is_default", True)),
                        fields=("company", "document_type"),
                        name="uq_purchase_template_default_type",
                    ),
                ],
            },
        ),
    ]
