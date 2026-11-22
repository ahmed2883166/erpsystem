# Generated for historical exchange rates; existing financial rows are untouched.
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("companies", "0001_initial")]

    operations = [
        migrations.CreateModel(
            name="ExchangeRate",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("rate_date", models.DateField()),
                ("rate", models.DecimalField(decimal_places=10, max_digits=20)),
                ("source", models.CharField(blank=True, max_length=120)),
                ("notes", models.CharField(blank=True, max_length=500)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="exchange_rates", to="companies.company")),
                ("currency", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="exchange_rates", to="companies.currency")),
            ],
            options={
                "verbose_name": "سعر صرف",
                "verbose_name_plural": "أسعار الصرف",
                "ordering": ("-rate_date", "currency__code"),
                "constraints": [
                    models.UniqueConstraint(fields=("company", "currency", "rate_date"), name="uq_exchange_rate_company_currency_date"),
                    models.CheckConstraint(condition=models.Q(("rate__gt", 0)), name="ck_exchange_rate_positive"),
                ],
                "indexes": [models.Index(fields=["company", "currency", "-rate_date"], name="fx_co_cur_date_idx")],
            },
        ),
    ]
