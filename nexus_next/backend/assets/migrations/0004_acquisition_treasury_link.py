import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("assets", "0003_disposal_treasury_link"),
        ("treasury", "0003_cash_flow_category"),
    ]
    operations = [
        migrations.AddField(
            model_name="fixedasset",
            name="acquisition_treasury_transaction",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="fixed_asset_acquisition",
                to="treasury.treasurytransaction",
            ),
        ),
    ]
