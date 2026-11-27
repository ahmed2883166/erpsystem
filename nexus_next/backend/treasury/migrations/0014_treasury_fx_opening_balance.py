from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("treasury", "0013_treasury_cheque"),
    ]

    operations = [
        migrations.AddField(
            model_name="treasuryaccount",
            name="opening_foreign_amount",
            field=models.DecimalField(decimal_places=4, default=0, max_digits=20),
        ),
        migrations.AddField(
            model_name="treasuryaccount",
            name="opening_exchange_rate",
            field=models.DecimalField(blank=True, decimal_places=10, max_digits=20, null=True),
        ),
        migrations.AddField(
            model_name="treasuryaccount",
            name="opening_exchange_rate_date",
            field=models.DateField(blank=True, null=True),
        ),
    ]
