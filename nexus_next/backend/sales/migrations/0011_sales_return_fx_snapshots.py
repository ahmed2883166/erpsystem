from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("sales", "0010_sales_return_reversal"),
    ]

    operations = [
        migrations.AddField(
            model_name="salesreturn",
            name="transaction_net_total",
            field=models.DecimalField(blank=True, decimal_places=4, max_digits=20, null=True),
        ),
        migrations.AddField(
            model_name="salesreturn",
            name="transaction_tax_total",
            field=models.DecimalField(blank=True, decimal_places=4, max_digits=20, null=True),
        ),
        migrations.AddField(
            model_name="salesreturn",
            name="transaction_grand_total",
            field=models.DecimalField(blank=True, decimal_places=4, max_digits=20, null=True),
        ),
        migrations.AddField(
            model_name="salesreturnline",
            name="transaction_net_amount",
            field=models.DecimalField(blank=True, decimal_places=4, max_digits=20, null=True),
        ),
        migrations.AddField(
            model_name="salesreturnline",
            name="transaction_tax_amount",
            field=models.DecimalField(blank=True, decimal_places=4, max_digits=20, null=True),
        ),
        migrations.AddField(
            model_name="salesreturnline",
            name="transaction_total_amount",
            field=models.DecimalField(blank=True, decimal_places=4, max_digits=20, null=True),
        ),
    ]
