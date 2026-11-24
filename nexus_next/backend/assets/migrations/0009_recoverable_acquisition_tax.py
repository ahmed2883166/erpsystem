from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("accounting", "0011_ledger_reporting_indexes"),
        ("assets", "0008_asset_vendor_invoice_and_payments"),
    ]

    operations = [
        migrations.AddField(
            model_name="fixedasset",
            name="recoverable_tax_amount",
            field=models.DecimalField(decimal_places=4, default=0, max_digits=20),
        ),
    ]
