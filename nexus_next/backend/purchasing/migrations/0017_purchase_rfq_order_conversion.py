import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("purchasing", "0016_purchase_rfq_and_quotations"),
    ]

    operations = [
        migrations.AddField(
            model_name="purchaserfq",
            name="converted_order",
            field=models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="source_rfq", to="purchasing.purchaseorder"),
        ),
    ]
