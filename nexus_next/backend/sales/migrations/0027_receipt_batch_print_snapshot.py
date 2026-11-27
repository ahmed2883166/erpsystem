from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("sales", "0026_sales_print_template_snapshots"),
    ]

    operations = [
        migrations.AddField(
            model_name="customerreceiptbatch",
            name="print_template_snapshot",
            field=models.JSONField(blank=True, default=dict, editable=False),
        ),
    ]
