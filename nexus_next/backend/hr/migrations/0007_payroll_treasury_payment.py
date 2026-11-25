import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("hr", "0006_payroll_branch_postings"),
        ("treasury", "0001_initial"),
    ]

    operations = [
        migrations.AlterField(
            model_name="payrollcycle",
            name="status",
            field=models.CharField(
                choices=[
                    ("draft", "مسودة"),
                    ("calculated", "تم الاحتساب"),
                    ("review", "تحت المراجعة"),
                    ("approved", "معتمدة"),
                    ("posted", "مرحّلة"),
                    ("payment_pending", "بانتظار اعتماد الصرف"),
                    ("paid", "تم الصرف"),
                ],
                default="draft",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="payrollposting",
            name="payment_treasury_transaction",
            field=models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="payroll_posting", to="treasury.treasurytransaction"),
        ),
    ]
