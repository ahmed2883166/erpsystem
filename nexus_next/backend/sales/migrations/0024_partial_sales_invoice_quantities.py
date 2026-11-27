from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("sales", "0023_customer_refunds"),
    ]

    operations = [
        migrations.AlterField(
            model_name="salesinvoiceline",
            name="delivery_line",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="sales_invoice_lines",
                to="inventory.stockmovementline",
            ),
        ),
    ]
