from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("sales", "0025_multiple_invoices_per_delivery"),
    ]

    operations = [
        migrations.AddField(
            model_name="salesdelivery",
            name="print_template_snapshot",
            field=models.JSONField(blank=True, default=dict, editable=False),
        ),
        migrations.AddField(
            model_name="customerreceipt",
            name="print_template_snapshot",
            field=models.JSONField(blank=True, default=dict, editable=False),
        ),
        migrations.AddField(
            model_name="salesreturn",
            name="print_template_snapshot",
            field=models.JSONField(blank=True, default=dict, editable=False),
        ),
    ]
