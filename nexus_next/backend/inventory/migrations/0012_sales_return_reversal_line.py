import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0011_supplier_return_reversal_movement"),
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
                    ("sal_rev", "عكس مردود مبيعات"),
                    ("transfer", "تحويل"),
                    ("adjustment", "تسوية جرد"),
                ],
                max_length=12,
            ),
        ),
        migrations.AddField(
            model_name="stockmovementline",
            name="reversal_of_line",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="reversal_lines",
                to="inventory.stockmovementline",
            ),
        ),
    ]
