from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("treasury", "0009_treasury_fx_revaluation_reversal"),
    ]

    operations = [
        migrations.AddField(
            model_name="treasurytransaction",
            name="reversal_of",
            field=models.OneToOneField(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="reversal_transaction", to="treasury.treasurytransaction",
            ),
        ),
    ]
