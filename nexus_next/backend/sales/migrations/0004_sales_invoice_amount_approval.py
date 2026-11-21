from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("sales", "0003_sales_discount_approval_snapshot"),
    ]

    operations = [
        migrations.AddField(
            model_name="salesinvoice",
            name="invoice_approval_limit",
            field=models.DecimalField(blank=True, decimal_places=4, max_digits=20, null=True),
        ),
    ]
