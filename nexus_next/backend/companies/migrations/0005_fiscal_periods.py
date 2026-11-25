import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    dependencies = [
        ("companies", "0004_platform_owner"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="FiscalPeriod",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=40)),
                ("start_date", models.DateField()),
                ("end_date", models.DateField()),
                ("status", models.CharField(choices=[("open", "مفتوحة"), ("closed", "مغلقة")], default="open", max_length=8)),
                ("closed_at", models.DateTimeField(blank=True, null=True)),
                ("reopened_at", models.DateTimeField(blank=True, null=True)),
                ("reopen_reason", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("closed_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="closed_fiscal_periods", to=settings.AUTH_USER_MODEL)),
                ("fiscal_year", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="periods", to="companies.fiscalyear")),
                ("reopened_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="reopened_fiscal_periods", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ("start_date", "id")},
        ),
        migrations.AddConstraint(
            model_name="fiscalperiod",
            constraint=models.UniqueConstraint(fields=("fiscal_year", "name"), name="uq_fiscal_period_year_name"),
        ),
        migrations.AddConstraint(
            model_name="fiscalperiod",
            constraint=models.CheckConstraint(condition=Q(end_date__gte=models.F("start_date")), name="ck_fiscal_period_dates"),
        ),
        migrations.AddIndex(
            model_name="fiscalperiod",
            index=models.Index(fields=["fiscal_year", "status", "start_date"], name="fiscal_period_status_idx"),
        ),
    ]
