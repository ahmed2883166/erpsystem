from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("purchasing", "0007_supplier_payment_historical_fx_carrying")]

    operations = [
        migrations.AlterField(
            model_name="supplierpayment",
            name="status",
            field=models.CharField(
                choices=[("draft", "مسودة"), ("posted", "مرحّل"), ("reversed", "معكوس")],
                default="draft",
                max_length=10,
            ),
        ),
    ]
