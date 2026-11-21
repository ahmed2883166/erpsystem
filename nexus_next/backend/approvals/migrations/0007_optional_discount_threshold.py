from django.db import migrations, models
import django.core.validators


class Migration(migrations.Migration):
    dependencies = [
        ("approvals", "0006_sales_invoice_amount_approval"),
    ]

    operations = [
        migrations.AlterField(
            model_name="discountapprovalpolicy",
            name="max_discount_percent",
            field=models.DecimalField(
                blank=True, decimal_places=4, max_digits=7, null=True,
                validators=[
                    django.core.validators.MinValueValidator(0),
                    django.core.validators.MaxValueValidator(100),
                ],
                help_text="الحد الأقصى للخصم المسموح دون موافقة مدير الشركة؛ اتركه فارغًا لتعطيله.",
            ),
        ),
    ]
