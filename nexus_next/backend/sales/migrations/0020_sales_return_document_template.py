from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("sales", "0019_sales_delivery_order_snapshot"),
    ]

    operations = [
        migrations.AlterField(
            model_name="salesdocumenttemplate",
            name="document_type",
            field=models.CharField(
                choices=[
                    ("quotation", "عرض سعر"),
                    ("invoice", "فاتورة مبيعات"),
                    ("delivery", "إذن تسليم"),
                    ("receipt", "سند قبض"),
                    ("return", "مردود مبيعات"),
                ],
                max_length=16,
            ),
        ),
    ]
