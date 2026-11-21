from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("hr", "0003_employee_component_default_value")]

    operations = [
        migrations.AddField(
            model_name="attendancerecord",
            name="friday_worked",
            field=models.BooleanField(default=False),
        ),
        migrations.AlterField(
            model_name="payrollcomponent",
            name="method",
            field=models.CharField(
                choices=[
                    ("fixed", "مبلغ ثابت"),
                    ("percent_basic", "نسبة من الأساسي"),
                    ("per_day", "حسب الأيام"),
                    ("per_hour", "حسب الساعات"),
                    ("friday_double_day", "عمل الجمعة بضعف اليومية"),
                    ("manual", "إدخال يدوي"),
                ],
                default="fixed",
                max_length=20,
            ),
        ),
    ]
