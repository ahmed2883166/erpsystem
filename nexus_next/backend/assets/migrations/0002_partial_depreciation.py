from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    dependencies = [("assets", "0001_initial")]
    operations = [
        migrations.AddField(
            model_name="assetdepreciation",
            name="is_partial_period",
            field=models.BooleanField(default=False),
        ),
        migrations.RemoveConstraint(
            model_name="assetdepreciation",
            name="uq_asset_depr_period",
        ),
        migrations.AddConstraint(
            model_name="assetdepreciation",
            constraint=models.UniqueConstraint(
                fields=("asset", "period_date"),
                condition=Q(is_partial_period=False),
                name="uq_asset_depr_period",
            ),
        ),
    ]
