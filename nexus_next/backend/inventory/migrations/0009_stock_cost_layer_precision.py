import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("inventory", "0008_fifo_cost_lineage")]

    operations = [
        migrations.AlterField(
            model_name="stockcostlayer",
            name="unit_cost",
            field=models.DecimalField(
                decimal_places=8, max_digits=20,
                validators=[django.core.validators.MinValueValidator(0)],
            ),
        ),
    ]
