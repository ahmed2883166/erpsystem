from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0018_stock_line_product_movement_index"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="stockmovement",
            index=models.Index(
                fields=["status", "movement_date", "posted_at", "id"],
                name="inv_move_card_order_idx",
            ),
        ),
    ]
