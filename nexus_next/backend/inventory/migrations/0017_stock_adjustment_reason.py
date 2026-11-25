from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0016_one_open_inventory_count"),
    ]

    operations = [
        migrations.AddField(
            model_name="stockmovement",
            name="adjustment_reason",
            field=models.CharField(
                blank=True,
                choices=[
                    ("count", "جرد فعلي"),
                    ("damage", "تالف"),
                    ("expired", "منتهي الصلاحية"),
                    ("write_off", "شطب أو إعدام"),
                    ("other", "أخرى"),
                ],
                max_length=16,
            ),
        ),
    ]
