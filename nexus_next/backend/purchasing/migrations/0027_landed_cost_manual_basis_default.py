from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("purchasing", "0026_landed_cost_reversal_journals"),
    ]

    operations = [
        migrations.AlterField(
            model_name="landedcostallocation",
            name="basis",
            field=models.CharField(
                choices=[
                    ("value", "القيمة"),
                    ("quantity", "الكمية"),
                    ("weight", "الوزن"),
                    ("volume", "الحجم"),
                    ("manual", "توزيع يدوي"),
                ],
                default="manual",
                max_length=12,
            ),
        ),
    ]
