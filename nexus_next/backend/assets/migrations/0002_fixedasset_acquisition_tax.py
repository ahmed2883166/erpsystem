from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("accounting", "0001_initial"),
        ("assets", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="fixedasset",
            name="acquisition_net_amount",
            field=models.DecimalField(blank=True, decimal_places=4, max_digits=20, null=True),
        ),
        migrations.AddField(
            model_name="fixedasset",
            name="nonrecoverable_tax_amount",
            field=models.DecimalField(decimal_places=4, default=0, max_digits=20),
        ),
        migrations.AddField(
            model_name="fixedasset",
            name="acquisition_tax_code",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="fixed_asset_acquisitions", to="accounting.taxcode",
            ),
        ),
    ]
