import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("accounting", "0004_tax_code_configuration"),
        ("sales", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="salesinvoiceline",
            name="tax_code",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="sales_invoice_lines", to="accounting.taxcode"),
        ),
    ]
