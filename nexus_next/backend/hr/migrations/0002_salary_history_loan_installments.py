import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("hr", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="EmployeeSalaryHistory",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("effective_date", models.DateField()),
                ("previous_salary", models.DecimalField(decimal_places=4, max_digits=18)),
                ("new_salary", models.DecimalField(decimal_places=4, max_digits=18)),
                ("change_type", models.CharField(choices=[("annual_raise", "زيادة سنوية"), ("promotion", "ترقية"), ("adjustment", "تعديل")], default="adjustment", max_length=16)),
                ("reason", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("changed_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL)),
                ("employee", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="salary_history", to="hr.employee")),
            ],
            options={"ordering": ("-effective_date", "-id"), "constraints": [models.CheckConstraint(condition=models.Q(("new_salary__gte", 0), ("previous_salary__gte", 0)), name="ck_hr_salary_nonnegative")]},
        ),
        migrations.CreateModel(
            name="LoanInstallment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("due_date", models.DateField()),
                ("amount", models.DecimalField(decimal_places=4, max_digits=18)),
                ("paid_amount", models.DecimalField(decimal_places=4, default=0, max_digits=18)),
                ("paid_date", models.DateField(blank=True, null=True)),
                ("status", models.CharField(choices=[("due", "مستحق"), ("paid", "مسدد"), ("partial", "مسدد جزئيًا"), ("waived", "معفى")], default="due", max_length=12)),
                ("loan", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="installments", to="hr.employeeloan")),
                ("payroll_cycle", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="loan_installments", to="hr.payrollcycle")),
            ],
            options={"ordering": ("due_date", "id"), "constraints": [models.UniqueConstraint(fields=("loan", "due_date"), name="uq_hr_loan_installment_date"), models.CheckConstraint(condition=models.Q(("amount__gt", 0), ("paid_amount__gte", 0), ("paid_amount__lte", models.F("amount"))), name="ck_hr_loan_installment_amount")]},
        ),
    ]
