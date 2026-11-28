from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("approvals", "0016_purchase_order_approval"),
    ]

    operations = [
        migrations.AddField(
            model_name="salesinvoiceapproval",
            name="requested_credit_limit",
            field=models.DecimalField(blank=True, decimal_places=4, max_digits=20, null=True),
        ),
        migrations.AddField(
            model_name="salesinvoiceapproval",
            name="requested_credit_exposure",
            field=models.DecimalField(blank=True, decimal_places=4, max_digits=20, null=True),
        ),
        migrations.AddField(
            model_name="salesinvoiceapproval",
            name="requested_credit_amount",
            field=models.DecimalField(blank=True, decimal_places=4, max_digits=20, null=True),
        ),
    ]
