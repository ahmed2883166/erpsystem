from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0010_alter_stockmovement_movement_type"),
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
                    ("sup_rev", "عكس مرتجع مورد"),
                    ("transfer", "تحويل"),
                    ("adjustment", "تسوية جرد"),
                ],
                max_length=12,
            ),
        ),
    ]
