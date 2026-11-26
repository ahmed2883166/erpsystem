from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("sales", "0022_customer_refunds"),
    ]

    operations = [
        migrations.AlterField(
            model_name="salesdocumenttemplate",
            name="document_type",
            field=models.CharField(
                choices=[
                    ("quotation", "عرض سعر"),
                    ("invoice", "فاتورة مبيعات"),
                    ("delivery", "إذن تسليم"),
                    ("receipt", "سند قبض"),
                    ("return", "مردود مبيعات"),
                    ("purchase_req", "طلب احتياج"),
                    ("purchase_rfq", "طلب عروض أسعار"),
                    ("supplier_quote", "عرض مورد"),
                    ("purchase_order", "أمر شراء"),
                    ("service_accept", "محضر إنجاز خدمة"),
                    ("purchase_receipt", "إذن استلام مشتريات"),
                    ("purchase_invoice", "فاتورة مورد"),
                    ("supplier_payment", "سند صرف مورد"),
                    ("supplier_return", "مرتجع مورد"),
                    ("supplier_refund", "استرداد من مورد"),
                ],
                max_length=16,
            ),
        ),
    ]
