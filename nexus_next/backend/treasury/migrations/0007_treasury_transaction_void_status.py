from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("treasury", "0006_treasury_fx_position_revaluation"),
    ]

    operations = [
        migrations.AlterField(
            model_name="treasurytransaction",
            name="status",
            field=models.CharField(
                choices=[("draft", "مسودة"), ("posted", "مرحّل"), ("void", "ملغي")],
                default="draft",
                max_length=8,
            ),
        ),
    ]
