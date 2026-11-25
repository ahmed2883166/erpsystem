import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("companies", "0001_initial"),
        ("treasury", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="treasurytransaction",
            name="transaction_currency",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="treasury_transactions", to="companies.currency",
            ),
        ),
        migrations.AddField(
            model_name="treasurytransaction",
            name="transaction_amount",
            field=models.DecimalField(blank=True, decimal_places=4, max_digits=20, null=True),
        ),
        migrations.AddField(
            model_name="treasurytransaction",
            name="exchange_rate",
            field=models.DecimalField(blank=True, decimal_places=10, max_digits=20, null=True),
        ),
        migrations.AddField(
            model_name="treasurytransaction",
            name="exchange_rate_date",
            field=models.DateField(blank=True, null=True),
        ),
    ]
