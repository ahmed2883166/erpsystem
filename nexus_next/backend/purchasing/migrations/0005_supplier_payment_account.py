import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("accounting", "0005_distinct_tax_insurance_roles"),
        ("purchasing", "0004_purchaseinvoice_taxcode"),
    ]

    operations = [
        migrations.AddField(
            model_name="supplierpayment",
            name="payment_account",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="supplier_payments", to="accounting.chartofaccount"),
        ),
    ]
