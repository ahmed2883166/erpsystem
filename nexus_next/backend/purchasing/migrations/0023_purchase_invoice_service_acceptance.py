import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("purchasing", "0022_purchase_service_acceptance"),
    ]

    operations = [
        migrations.AddField(
            model_name="purchaseinvoice",
            name="service_acceptance",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="supplier_invoice",
                to="purchasing.purchaseserviceacceptance",
            ),
        ),
    ]
