from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("sales", "0024_partial_sales_invoice_quantities"),
    ]

    operations = [
        migrations.AlterField(
            model_name="salesinvoice",
            name="delivery",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="invoices",
                to="sales.salesdelivery",
            ),
        ),
    ]
