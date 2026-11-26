import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("sales", "0015_service_sales_invoices"),
        ("inventory", "0018_stock_line_product_movement_index"),
    ]

    operations = [
        migrations.AlterField(
            model_name="salesreturn",
            name="warehouse",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="sales_returns", to="inventory.warehouse"),
        ),
        migrations.AlterField(
            model_name="salesreturn",
            name="stock_movement",
            field=models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="sales_return", to="inventory.stockmovement"),
        ),
        migrations.AddField(
            model_name="salesreturn",
            name="branch",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="sales_returns", to="companies.branch"),
        ),
        migrations.AlterField(
            model_name="salesreturnline",
            name="movement_line",
            field=models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="sales_return_line", to="inventory.stockmovementline"),
        ),
    ]
