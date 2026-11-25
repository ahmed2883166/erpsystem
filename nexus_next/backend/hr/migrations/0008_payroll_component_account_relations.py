import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("hr", "0007_payroll_treasury_payment"),
    ]

    operations = [
        migrations.AlterField(
            model_name="payrollcomponent",
            name="expense_account",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="payroll_components_expense",
                to="accounting.chartofaccount",
            ),
        ),
        migrations.AlterField(
            model_name="payrollcomponent",
            name="payable_account",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="payroll_components_payable",
                to="accounting.chartofaccount",
            ),
        ),
    ]
