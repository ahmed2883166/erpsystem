import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("purchasing", "0025_landed_cost_source_journal"),
    ]

    operations = [
        migrations.AddField(
            model_name="landedcostallocation",
            name="source_reversal_journal_entry",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="landed_cost_source_allocation_reversal",
                to="accounting.journalentry",
            ),
        ),
        migrations.AddField(
            model_name="landedcostdistribution",
            name="reversal_journal_entry",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="landed_cost_distribution_reversal",
                to="accounting.journalentry",
            ),
        ),
    ]
