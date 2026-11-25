from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("treasury", "0007_treasury_transaction_void_status"),
    ]

    operations = [
        migrations.AlterField(
            model_name="treasuryaccount",
            name="currency_code",
            field=models.CharField(blank=True, default="", max_length=3),
        ),
    ]
