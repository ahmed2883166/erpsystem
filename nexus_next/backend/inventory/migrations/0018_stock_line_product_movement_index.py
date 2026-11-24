from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0017_stock_adjustment_reason"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="stockmovementline",
            index=models.Index(
                fields=["product", "movement"],
                name="inv_stock_line_product_move_idx",
            ),
        ),
    ]
