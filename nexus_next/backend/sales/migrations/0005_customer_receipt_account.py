import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("accounting", "0005_distinct_tax_insurance_roles"),
        ("sales", "0004_sales_invoice_amount_approval"),
    ]

    operations = [
        migrations.AddField(
            model_name="customerreceipt",
            name="payment_account",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="customer_receipts", to="accounting.chartofaccount"),
        ),
    ]
