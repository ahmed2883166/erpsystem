import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("purchasing", "0024_landed_cost_models"),
    ]

    operations = [
        migrations.AddField(
            model_name="landedcostallocation",
            name="source_journal_entry",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="landed_cost_source_allocation",
                to="accounting.journalentry",
            ),
        ),
    ]
