import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("sales", "0013_sales_quotations"),
    ]

    operations = [
        migrations.AddField(
            model_name="salesinvoice",
            name="print_template_snapshot",
            field=models.JSONField(blank=True, default=dict, editable=False),
        ),
        migrations.AddField(
            model_name="salesquotation",
            name="print_template_snapshot",
            field=models.JSONField(blank=True, default=dict, editable=False),
        ),
        migrations.CreateModel(
            name="SalesDocumentTemplate",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("document_type", models.CharField(choices=[("quotation", "عرض سعر"), ("invoice", "فاتورة مبيعات"), ("delivery", "إذن تسليم"), ("receipt", "سند قبض")], max_length=16)),
                ("name", models.CharField(max_length=80)),
                ("style", models.CharField(choices=[("classic", "Classic"), ("modern", "Modern"), ("compact", "Compact")], default="classic", max_length=12)),
                ("config", models.JSONField(blank=True, default=dict)),
                ("is_default", models.BooleanField(default=False)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="sales_document_templates", to="companies.company")),
            ],
            options={"ordering": ("document_type", "-is_default", "name")},
        ),
        migrations.AddConstraint(
            model_name="salesdocumenttemplate",
            constraint=models.UniqueConstraint(fields=("company", "document_type", "name"), name="uq_sales_template_company_type_name"),
        ),
        migrations.AddConstraint(
            model_name="salesdocumenttemplate",
            constraint=models.UniqueConstraint(condition=models.Q(("is_default", True)), fields=("company", "document_type"), name="uq_sales_template_default_type"),
        ),
        migrations.AddIndex(
            model_name="salesdocumenttemplate",
            index=models.Index(fields=["company", "document_type", "is_active"], name="sales_template_co_type_idx"),
        ),
    ]
