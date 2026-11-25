import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("assets", "0004_acquisition_treasury_link"),
    ]
    operations = [
        migrations.AddField(
            model_name="fixedasset",
            name="disposal_loss_account",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="fixed_asset_disposal_losses",
                to="accounting.chartofaccount",
            ),
        ),
    ]
