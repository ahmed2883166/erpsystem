from django.db import migrations, models
import django.core.validators


class Migration(migrations.Migration):
    dependencies = [("inventory", "0021_warehouse_status_audit")]

    operations = [
        migrations.AddField(
            model_name="product", name="unit_weight",
            field=models.DecimalField(decimal_places=6, default=0, max_digits=18, validators=[django.core.validators.MinValueValidator(0)]),
        ),
        migrations.AddField(
            model_name="product", name="unit_volume",
            field=models.DecimalField(decimal_places=6, default=0, max_digits=18, validators=[django.core.validators.MinValueValidator(0)]),
        ),
    ]
