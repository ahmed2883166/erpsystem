import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("assets", "0002_partial_depreciation"),
        ("treasury", "0002_bankreconciliation"),
    ]
    operations = [
        migrations.AddField(
            model_name="fixedasset",
            name="disposal_treasury_transaction",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="fixed_asset_disposal",
                to="treasury.treasurytransaction",
            ),
        ),
    ]
