from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("sales", "0008_customer_receipt_historical_fx_carrying")]

    operations = [
        migrations.AlterField(
            model_name="customerreceipt",
            name="status",
            field=models.CharField(
                choices=[("draft", "مسودة"), ("posted", "مرحّل"), ("reversed", "معكوس")],
                default="draft",
                max_length=10,
            ),
        ),
    ]
