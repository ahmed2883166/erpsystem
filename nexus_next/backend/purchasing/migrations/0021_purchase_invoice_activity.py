import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("companies", "0008_company_multi_activity"),
        ("purchasing", "0020_direct_expense_purchase_invoices"),
    ]

    operations = [
        migrations.AddField(
            model_name="purchaseinvoice",
            name="activity",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="purchase_invoices", to="companies.companyactivity",
            ),
        ),
    ]
