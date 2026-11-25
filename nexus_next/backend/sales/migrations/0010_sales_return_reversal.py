import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("sales", "0009_customer_receipt_reversed_status"),
        ("inventory", "0012_sales_return_reversal_line"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="salesreturn",
            name="reversal_stock_movement",
            field=models.OneToOneField(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="sales_return_reversal", to="inventory.stockmovement",
            ),
        ),
        migrations.AddField(
            model_name="salesreturn",
            name="reversal_journal_entry",
            field=models.OneToOneField(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="sales_return_reversal", to="accounting.journalentry",
            ),
        ),
        migrations.AddField(
            model_name="salesreturn",
            name="reversal_date",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="salesreturn",
            name="reversed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="salesreturn",
            name="reversed_by",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="reversed_sales_returns", to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name="salesreturn",
            name="status",
            field=models.CharField(
                choices=[("draft", "مسودة"), ("posted", "مرحّل"), ("reversed", "معكوس")],
                default="draft",
                max_length=10,
            ),
        ),
    ]
