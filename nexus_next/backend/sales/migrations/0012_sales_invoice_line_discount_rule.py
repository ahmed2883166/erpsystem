import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("customers", "0003_customer_discount_rules"),
        ("sales", "0011_sales_return_fx_snapshots"),
    ]

    operations = [
        migrations.AddField(
            model_name="salesinvoiceline",
            name="discount_rule",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="sales_invoice_lines",
                to="customers.customerdiscountrule",
            ),
        ),
    ]
