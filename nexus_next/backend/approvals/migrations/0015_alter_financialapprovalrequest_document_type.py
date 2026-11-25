from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("approvals", "0014_asset_financial_approvals"),
    ]

    operations = [
        migrations.AlterField(
            model_name="financialapprovalrequest",
            name="document_type",
            field=models.CharField(
                choices=[
                    ("purchase_invoice", "فاتورة مشتريات"),
                    ("supplier_payment", "سند صرف مورد"),
                    ("treasury_payment", "سند صرف خزينة"),
                    ("asset_purchase_invoice", "فاتورة شراء أصل ثابت"),
                    ("asset_supplier_payment", "سند سداد أصل ثابت"),
                ],
                max_length=24,
            ),
        ),
    ]
