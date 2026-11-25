from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("sales", "0007_sales_fx_snapshots")]

    operations = [
        migrations.AddField(
            model_name="customerreceipt",
            name="historical_carrying_base_amount",
            field=models.DecimalField(blank=True, decimal_places=4, max_digits=20, null=True),
        ),
    ]
