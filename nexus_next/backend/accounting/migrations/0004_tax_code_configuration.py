import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("accounting", "0003_inventory_account_roles"),
        ("companies", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="TaxCode",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("country", models.CharField(choices=[("EG", "مصر"), ("SA", "السعودية")], max_length=2)),
                ("code", models.CharField(max_length=32)),
                ("name_ar", models.CharField(max_length=160)),
                ("category", models.CharField(choices=[
                    ("standard", "خاضع بالنسبة القياسية"),
                    ("zero_rated", "نسبة صفرية"),
                    ("exempt", "معفى"),
                    ("out_of_scope", "خارج النطاق"),
                    ("reverse_charge", "احتساب عكسي"),
                ], default="standard", max_length=20)),
                ("rate", models.DecimalField(decimal_places=4, default=0, max_digits=7)),
                ("effective_from", models.DateField()),
                ("effective_to", models.DateField(blank=True, null=True)),
                ("input_tax_recoverable", models.BooleanField(default=True)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="tax_codes", to="companies.company")),
            ],
            options={"ordering": ("code", "effective_from")},
        ),
        migrations.AddConstraint(
            model_name="taxcode",
            constraint=models.UniqueConstraint(fields=("company", "code", "effective_from"), name="uq_tax_code_company_start"),
        ),
        migrations.AddConstraint(
            model_name="taxcode",
            constraint=models.CheckConstraint(condition=models.Q(("rate__gte", 0), ("rate__lte", 100)), name="ck_tax_code_rate_range"),
        ),
        migrations.AddConstraint(
            model_name="taxcode",
            constraint=models.CheckConstraint(condition=models.Q(("effective_to__isnull", True), ("effective_to__gte", models.F("effective_from")), _connector="OR"), name="ck_tax_code_effective_dates"),
        ),
        migrations.AddIndex(
            model_name="taxcode",
            index=models.Index(fields=["company", "country", "is_active"], name="acct_tax_company_active_idx"),
        ),
    ]
