from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("approvals", "0007_optional_discount_threshold"),
    ]

    operations = [
        migrations.AlterField(
            model_name="salesinvoiceapproval",
            name="requested_limit",
            field=models.DecimalField(blank=True, decimal_places=4, max_digits=7, null=True),
        ),
    ]
