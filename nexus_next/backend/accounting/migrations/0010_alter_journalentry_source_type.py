from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("accounting", "0009_fx_subledger_revaluation")]

    operations = [
        migrations.AlterField(
            model_name="journalentry",
            name="source_type",
            field=models.CharField(
                choices=[
                    ("manual", "يدوي"),
                    ("sales", "مبيعات"),
                    ("purchases", "مشتريات"),
                    ("payroll", "مرتبات"),
                    ("inventory", "مخزون"),
                    ("assets", "أصول ثابتة"),
                    ("opening", "افتتاحي"),
                    ("closing", "إقفال"),
                    ("fx_revaluation", "إعادة تقييم عملة"),
                ],
                default="manual",
                max_length=16,
            ),
        ),
    ]
