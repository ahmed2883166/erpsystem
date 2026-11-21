import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("hr", "0005_statutory_payroll_methods"),
        ("accounting", "0005_distinct_tax_insurance_roles"),
    ]

    operations = [
        migrations.AddField(
            model_name="employeeloan",
            name="receivable_account",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, to="accounting.chartofaccount"),
        ),
        migrations.CreateModel(
            name="PayrollPosting",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("branch", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="payroll_postings", to="companies.branch")),
                ("cycle", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="postings", to="hr.payrollcycle")),
                ("journal_entry", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="payroll_posting", to="accounting.journalentry")),
                ("payment_journal_entry", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="payroll_payment_posting", to="accounting.journalentry")),
            ],
            options={"ordering": ("branch__code",), "constraints": [models.UniqueConstraint(fields=("cycle", "branch"), name="uq_hr_payroll_posting_branch")]},
        ),
    ]
