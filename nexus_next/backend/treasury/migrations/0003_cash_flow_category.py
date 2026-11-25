from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("treasury", "0002_bankreconciliation_bankstatementline"),
    ]
    operations = [
        migrations.AddField(
            model_name="treasurytransaction",
            name="cash_flow_category",
            field=models.CharField(
                choices=[("operating", "تشغيلي"), ("investing", "استثماري"), ("financing", "تمويلي")],
                default="operating",
                max_length=12,
            ),
        ),
    ]
