from django.db import migrations, models
import django.db.models.deletion


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
        migrations.AddField(
            model_name="fixedasset",
            name="acquisition_tax_journal",
            field=models.OneToOneField(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="fixed_asset_acquisition_tax_adjustment",
                to="accounting.journalentry",
            ),
        ),
    ]
