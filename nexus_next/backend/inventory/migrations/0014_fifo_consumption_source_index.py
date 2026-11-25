from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0013_inventory_count"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="stockcostconsumption",
            index=models.Index(
                fields=["source_layer", "movement_line"],
                name="inv_consume_source_move_idx",
            ),
        ),
    ]
