from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("treasury", "0009_treasury_fx_revaluation_reversal"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations = [
        migrations.AlterField(
            model_name="treasurytransaction",
            name="status",
            field=models.CharField(
                choices=[("draft", "مسودة"), ("posted", "مرحّل"), ("void", "ملغي"), ("reversed", "معكوس")],
                default="draft",
                max_length=8,
            ),
        ),
        migrations.AddField(
            model_name="treasurytransaction", name="reversal_journal_entry",
            field=models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="treasury_transaction_reversal", to="accounting.journalentry"),
        ),
        migrations.AddField(model_name="treasurytransaction", name="reversed_at", field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(
            model_name="treasurytransaction", name="reversed_by",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="treasury_transactions_reversed", to=settings.AUTH_USER_MODEL),
        ),
    ]
