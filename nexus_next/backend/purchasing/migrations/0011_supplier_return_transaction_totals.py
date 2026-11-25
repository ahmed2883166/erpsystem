from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("purchasing", "0010_supplier_returns"),
    ]

    operations = [
        migrations.AddField(
            model_name="supplierreturn",
            name="transaction_net_amount",
            field=models.DecimalField(blank=True, decimal_places=4, max_digits=20, null=True),
        ),
        migrations.AddField(
            model_name="supplierreturn",
            name="transaction_tax_amount",
            field=models.DecimalField(blank=True, decimal_places=4, max_digits=20, null=True),
        ),
        migrations.AddField(
            model_name="supplierreturn",
            name="transaction_grand_total",
            field=models.DecimalField(blank=True, decimal_places=4, max_digits=20, null=True),
        ),
    ]
