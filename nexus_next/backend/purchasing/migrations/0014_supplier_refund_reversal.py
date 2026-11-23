import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("purchasing", "0013_supplier_refund"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="supplierrefund",
            name="reversal_journal_entry",
            field=models.OneToOneField(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="supplier_refund_reversal", to="accounting.journalentry",
            ),
        ),
        migrations.AddField(
            model_name="supplierrefund",
            name="reversal_date",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="supplierrefund",
            name="reversed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="supplierrefund",
            name="reversed_by",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="reversed_supplier_refunds", to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name="supplierrefund",
            name="status",
            field=models.CharField(
                choices=[("draft", "مسودة"), ("posted", "مرحّل"), ("reversed", "معكوس")],
                default="draft",
                max_length=10,
            ),
        ),
    ]
