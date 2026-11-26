from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("sales", "0018_sales_delivery_order_allocations"),
    ]

    operations = [
        migrations.AddField(
            model_name="salesdelivery",
            name="order_snapshot",
            field=models.JSONField(blank=True, default=dict, editable=False),
        ),
    ]
