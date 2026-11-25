from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("customers", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="customer",
            name="default_discount_percent",
            field=models.DecimalField(
                decimal_places=4,
                default=0,
                max_digits=7,
                validators=[MinValueValidator(0), MaxValueValidator(100)],
            ),
        ),
        migrations.AddField(
            model_name="customer",
            name="discount_approval_limit",
            field=models.DecimalField(
                blank=True,
                decimal_places=4,
                help_text="Maximum customer discount allowed without manager approval.",
                max_digits=7,
                null=True,
                validators=[MinValueValidator(0), MaxValueValidator(100)],
            ),
        ),
    ]
