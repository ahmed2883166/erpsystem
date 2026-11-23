import django.core.validators
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("customers", "0003_customer_discount_rules"),
        ("sales", "0012_sales_invoice_line_discount_rule"),
    ]

    operations = [
        migrations.AddField(
            model_name="salesdelivery",
            name="quotation_snapshot",
            field=models.JSONField(blank=True, default=dict, editable=False),
        ),
        migrations.CreateModel(
            name="SalesQuotation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("quotation_number", models.CharField(max_length=80)),
                ("quotation_date", models.DateField()),
                ("valid_until", models.DateField()),
                ("status", models.CharField(choices=[("draft", "مسودة"), ("sent", "مرسل"), ("accepted", "مقبول"), ("rejected", "مرفوض"), ("expired", "منتهي"), ("converted", "تم تحويله")], default="draft", max_length=12)),
                ("net_total", models.DecimalField(decimal_places=4, default=0, max_digits=20)),
                ("discount_total", models.DecimalField(decimal_places=4, default=0, max_digits=20)),
                ("tax_total", models.DecimalField(decimal_places=4, default=0, max_digits=20)),
                ("grand_total", models.DecimalField(decimal_places=4, default=0, max_digits=20)),
                ("notes", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("sent_at", models.DateTimeField(blank=True, null=True)),
                ("accepted_at", models.DateTimeField(blank=True, null=True)),
                ("converted_at", models.DateTimeField(blank=True, null=True)),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="sales_quotations", to="companies.company")),
                ("converted_delivery", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="source_quotation", to="sales.salesdelivery")),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="created_sales_quotations", to="accounts.user")),
                ("customer", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="sales_quotations", to="customers.customer")),
            ],
            options={"ordering": ("-quotation_date", "-id")},
        ),
        migrations.CreateModel(
            name="SalesQuotationLine",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("quantity", models.DecimalField(decimal_places=4, max_digits=18, validators=[django.core.validators.MinValueValidator(0.0001)])),
                ("unit_price", models.DecimalField(decimal_places=4, max_digits=18, validators=[django.core.validators.MinValueValidator(0)])),
                ("tax_rate", models.DecimalField(decimal_places=4, default=0, max_digits=7, validators=[django.core.validators.MinValueValidator(0), django.core.validators.MaxValueValidator(100)])),
                ("discount_percent", models.DecimalField(decimal_places=4, default=0, max_digits=7, validators=[django.core.validators.MinValueValidator(0), django.core.validators.MaxValueValidator(100)])),
                ("discount_amount", models.DecimalField(decimal_places=4, default=0, max_digits=20)),
                ("net_amount", models.DecimalField(decimal_places=4, default=0, max_digits=20)),
                ("tax_amount", models.DecimalField(decimal_places=4, default=0, max_digits=20)),
                ("total_amount", models.DecimalField(decimal_places=4, default=0, max_digits=20)),
                ("discount_rule", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="sales_quotation_lines", to="customers.customerdiscountrule")),
                ("product", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="sales_quotation_lines", to="inventory.product")),
                ("quotation", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="lines", to="sales.salesquotation")),
                ("tax_code", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="sales_quotation_lines", to="accounting.taxcode")),
            ],
            options={"ordering": ("id",)},
        ),
        migrations.AddConstraint(
            model_name="salesquotation",
            constraint=models.UniqueConstraint(fields=("company", "quotation_number"), name="uq_sales_quote_company_no"),
        ),
        migrations.AddConstraint(
            model_name="salesquotation",
            constraint=models.CheckConstraint(condition=models.Q(("valid_until__gte", models.F("quotation_date"))), name="ck_sales_quote_validity"),
        ),
        migrations.AddIndex(
            model_name="salesquotation",
            index=models.Index(fields=["company", "status", "quotation_date"], name="sales_quote_co_status_idx"),
        ),
    ]
