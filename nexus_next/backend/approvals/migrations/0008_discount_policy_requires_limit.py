from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("approvals", "0007_optional_discount_threshold"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="discountapprovalpolicy",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(max_discount_percent__isnull=False)
                    | models.Q(max_sales_invoice_amount__isnull=False)
                ),
                name="discount_policy_has_limit",
            ),
        ),
    ]
