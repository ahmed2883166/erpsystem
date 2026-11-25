import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("accounting", "0001_initial"),
        ("inventory", "0011_supplier_return_reversal_movement"),
        ("purchasing", "0011_supplier_return_transaction_totals"),
    ]

    operations = [
        migrations.AlterField(
            model_name="supplierreturn",
            name="status",
            field=models.CharField(
                choices=[("draft", "مسودة"), ("posted", "مرحّل"), ("reversed", "معكوس")],
                default="draft",
                max_length=10,
            ),
        ),
        migrations.AddField(
            model_name="supplierreturn",
            name="reversal_date",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="supplierreturn",
            name="reversed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="supplierreturn",
            name="reversal_stock_movement",
            field=models.OneToOneField(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="supplier_return_reversal", to="inventory.stockmovement",
            ),
        ),
        migrations.AddField(
            model_name="supplierreturn",
            name="reversal_journal_entry",
            field=models.OneToOneField(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="supplier_return_reversal", to="accounting.journalentry",
            ),
        ),
        migrations.AddField(
            model_name="supplierreturnline",
            name="reversal_movement_line",
            field=models.OneToOneField(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="supplier_return_reversal_line", to="inventory.stockmovementline",
            ),
        ),
    ]
