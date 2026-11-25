from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("hr", "0004_friday_worked_component")]

    operations = [
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
                    ("employee_insurance", "تأمينات الموظف حسب القواعد"),
                    ("employer_insurance", "تأمينات صاحب العمل حسب القواعد"),
                    ("progressive_tax", "ضريبة شرائح حسب القواعد"),
                    ("manual", "إدخال يدوي"),
                ],
                default="fixed",
                max_length=20,
            ),
        ),
    ]
