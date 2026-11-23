import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("hr", "0010_employee_user"),
        ("treasury", "0007_treasury_transaction_void_status"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="employeeloan",
            name="disbursement_transaction",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="employee_loan",
                to="treasury.treasurytransaction",
            ),
        ),
        migrations.AddField(
            model_name="employeeloan",
            name="disbursed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="employeeloan",
            name="disbursed_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="disbursed_employee_loans",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name="employeeloan",
            name="status",
            field=models.CharField(
                choices=[
                    ("draft", "مسودة"),
                    ("pending", "بانتظار اعتماد الصرف"),
                    ("active", "سارية"),
                    ("settled", "مسددة"),
                    ("suspended", "موقوفة"),
                ],
                default="active",
                max_length=12,
            ),
        ),
    ]
