from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("assets", "0006_fixedasset_acquisition_tax"),
    ]

    operations = [
        migrations.AddField(
            model_name="fixedasset",
            name="category",
            field=models.CharField(
                choices=[
                    ("land", "أراضٍ"),
                    ("building", "مبانٍ"),
                    ("vehicle", "سيارات ووسائل نقل"),
                    ("furniture", "أثاث وتجهيزات"),
                    ("equipment", "معدات وآلات"),
                    ("computers", "أجهزة حاسب وتقنية"),
                    ("other", "أخرى"),
                ],
                default="other",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="fixedasset",
            name="location",
            field=models.CharField(blank=True, max_length=180),
        ),
        migrations.AddField(
            model_name="fixedasset",
            name="custodian",
            field=models.CharField(blank=True, max_length=180),
        ),
        migrations.AddField(
            model_name="fixedasset",
            name="serial_number",
            field=models.CharField(blank=True, max_length=100),
        ),
        migrations.AddField(
            model_name="fixedasset",
            name="warranty_end_date",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="fixedasset",
            name="supplier_name",
            field=models.CharField(blank=True, max_length=180),
        ),
        migrations.AddField(
            model_name="fixedasset",
            name="supplier_invoice",
            field=models.CharField(blank=True, max_length=100),
        ),
    ]
