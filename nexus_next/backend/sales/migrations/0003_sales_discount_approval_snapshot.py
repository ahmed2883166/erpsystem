from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("sales", "0002_salesinvoice_taxcode"),
    ]

    operations = [
        migrations.AddField(
            model_name="salesinvoice",
            name="discount_total",
            field=models.DecimalField(decimal_places=4, default=0, max_digits=20),
        ),
        migrations.AddField(
            model_name="salesinvoice",
            name="discount_approval_limit",
            field=models.DecimalField(blank=True, decimal_places=4, max_digits=7, null=True),
        ),
        migrations.AddField(
            model_name="salesinvoiceline",
            name="discount_percent",
            field=models.DecimalField(decimal_places=4, default=0, max_digits=7),
        ),
        migrations.AddField(
            model_name="salesinvoiceline",
            name="discount_amount",
            field=models.DecimalField(decimal_places=4, default=0, max_digits=20),
        ),
    ]
