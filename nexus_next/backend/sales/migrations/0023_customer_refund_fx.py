from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("companies", "0001_initial"),
        ("sales", "0022_customer_refunds"),
    ]

    operations = [
        migrations.AddField(
            model_name="customerrefund",
            name="transaction_currency",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="customer_refunds", to="companies.currency",
            ),
        ),
        migrations.AddField(
            model_name="customerrefund",
            name="transaction_amount",
            field=models.DecimalField(blank=True, decimal_places=4, max_digits=20, null=True),
        ),
        migrations.AddField(
            model_name="customerrefund",
            name="exchange_rate",
            field=models.DecimalField(blank=True, decimal_places=10, max_digits=20, null=True),
        ),
        migrations.AddField(
            model_name="customerrefund",
            name="exchange_rate_date",
            field=models.DateField(blank=True, null=True),
        ),
    ]
