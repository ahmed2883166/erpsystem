import django.core.validators
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("companies", "0003_companymembership_branch_scope"),
    ]

    operations = [
        migrations.CreateModel(
            name="Customer",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("code", models.CharField(max_length=40)),
                ("customer_type", models.CharField(choices=[("company", "شركة"), ("individual", "فرد")], default="company", max_length=12)),
                ("name_ar", models.CharField(max_length=180)),
                ("name_en", models.CharField(blank=True, max_length=180)),
                ("tax_number", models.CharField(blank=True, max_length=80)),
                ("phone", models.CharField(blank=True, max_length=40)),
                ("mobile", models.CharField(blank=True, max_length=40)),
                ("email", models.EmailField(blank=True, max_length=254)),
                ("address", models.TextField(blank=True)),
                ("credit_limit", models.DecimalField(decimal_places=2, default=0, max_digits=16, validators=[django.core.validators.MinValueValidator(0)])),
                ("payment_terms_days", models.PositiveSmallIntegerField(default=0)),
                ("notes", models.TextField(blank=True)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="customers", to="companies.company")),
            ],
            options={
                "verbose_name": "عميل",
                "verbose_name_plural": "العملاء",
                "ordering": ("name_ar", "code"),
                "indexes": [
                    models.Index(fields=["company", "is_active", "name_ar"], name="cust_co_active_name_idx"),
                    models.Index(fields=["company", "tax_number"], name="cust_co_tax_idx"),
                ],
                "constraints": [
                    models.UniqueConstraint(fields=("company", "code"), name="uq_customer_company_code"),
                ],
            },
        ),
    ]
