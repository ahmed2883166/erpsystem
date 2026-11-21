import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("accounting", "0004_tax_code_configuration"),
        ("purchasing", "0003_purchaseorder"),
    ]

    operations = [
        migrations.AddField(
            model_name="purchaseinvoiceline",
            name="tax_code",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="purchase_invoice_lines", to="accounting.taxcode"),
        ),
    ]
