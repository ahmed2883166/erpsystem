from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("hr", "0002_salary_history_loan_installments")]

    operations = [
        migrations.AlterField(
            model_name="employeecomponent",
            name="value",
            field=models.DecimalField(blank=True, decimal_places=4, max_digits=18, null=True),
        ),
    ]
