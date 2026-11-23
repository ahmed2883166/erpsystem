from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0009_stock_cost_layer_precision"),
    ]

    operations = [
        migrations.AlterField(
            model_name="stockmovement",
            name="movement_type",
            field=models.CharField(
                choices=[
                    ("receipt", "استلام"),
                    ("issue", "صرف"),
                    ("sup_return", "مرتجع مورد"),
                    ("transfer", "تحويل"),
                    ("adjustment", "تسوية جرد"),
                ],
                max_length=12,
            ),
        ),
    ]
