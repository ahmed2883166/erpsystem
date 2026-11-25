import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("inventory", "0005_stock_movement_journal_links")]

    operations = [
        migrations.AddField(
            model_name="stockmovementline",
            name="cost_amount",
            field=models.DecimalField(decimal_places=4, default=0, max_digits=20, validators=[django.core.validators.MinValueValidator(0)]),
        ),
    ]
