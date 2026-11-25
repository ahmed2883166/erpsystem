import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("purchasing", "0017_purchase_rfq_order_conversion"),
    ]

    operations = [
        migrations.AddField(
            model_name="purchasereceipt",
            name="journal_entry",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="purchase_receipt",
                to="accounting.journalentry",
            ),
        ),
    ]
