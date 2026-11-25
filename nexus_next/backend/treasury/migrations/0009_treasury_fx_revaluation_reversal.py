from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("treasury", "0008_company_base_currency_default"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="treasuryfxrevaluation",
            name="reversal_journal_entry",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="treasury_fx_revaluation_reversal",
                to="accounting.journalentry",
            ),
        ),
        migrations.AddField(
            model_name="treasuryfxrevaluation",
            name="reversed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="treasuryfxrevaluation",
            name="reversed_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="treasury_fx_revaluations_reversed",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
