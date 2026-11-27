from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("treasury", "0010_treasury_transaction_reversal"),
    ]

    operations = [
        migrations.AddField(
            model_name="bankstatementline",
            name="adjustment_transaction",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="bank_statement_adjustment",
                to="treasury.treasurytransaction",
            ),
        ),
    ]
