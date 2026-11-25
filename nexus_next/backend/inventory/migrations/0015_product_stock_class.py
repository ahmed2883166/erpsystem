from django.db import migrations, models


def clear_service_stock_class(apps, schema_editor):
    Product = apps.get_model("inventory", "Product")
    Product.objects.filter(product_type="service").update(stock_class=None)


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0014_fifo_consumption_source_index"),
    ]

    operations = [
        migrations.AddField(
            model_name="product",
            name="stock_class",
            field=models.CharField(
                blank=True,
                choices=[
                    ("raw_material", "مواد خام"),
                    ("finished_goods", "منتجات تامة"),
                    ("operating_supplies", "مستلزمات تشغيل"),
                    ("spare_parts", "قطع غيار"),
                ],
                default="finished_goods",
                max_length=20,
                null=True,
            ),
        ),
        migrations.RunPython(clear_service_stock_class, migrations.RunPython.noop),
    ]
