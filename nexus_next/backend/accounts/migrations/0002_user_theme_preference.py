# Generated for the per-user interface theme preference.
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="theme_preference",
            field=models.CharField(
                choices=[("light", "فاتح"), ("dark", "داكن"), ("system", "حسب الجهاز")],
                default="system",
                max_length=10,
            ),
        ),
    ]
