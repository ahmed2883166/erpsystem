import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("treasury", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="BankReconciliation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("statement_number", models.CharField(max_length=60)),
                ("statement_date", models.DateField()),
                ("opening_balance", models.DecimalField(decimal_places=4, max_digits=20)),
                ("closing_balance", models.DecimalField(decimal_places=4, max_digits=20)),
                ("status", models.CharField(choices=[("draft", "مسودة"), ("reconciled", "تمت التسوية")], default="draft", max_length=12)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("reconciled_at", models.DateTimeField(blank=True, null=True)),
                ("bank_account", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="reconciliations", to="treasury.treasuryaccount")),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="bank_reconciliations", to="companies.company")),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="created_bank_reconciliations", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ("-statement_date", "-id"), "constraints": [models.UniqueConstraint(fields=("company", "bank_account", "statement_number"), name="uq_bank_recon_statement_no")]},
        ),
        migrations.CreateModel(
            name="BankStatementLine",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("line_number", models.PositiveIntegerField()),
                ("transaction_date", models.DateField()),
                ("description", models.CharField(blank=True, max_length=500)),
                ("reference", models.CharField(blank=True, max_length=120)),
                ("amount", models.DecimalField(decimal_places=4, max_digits=20)),
                ("matched_at", models.DateTimeField(blank=True, null=True)),
                ("matched_transaction", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="bank_statement_matches", to="treasury.treasurytransaction")),
                ("reconciliation", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="lines", to="treasury.bankreconciliation")),
            ],
            options={"ordering": ("line_number", "id"), "constraints": [
                models.UniqueConstraint(fields=("reconciliation", "line_number"), name="uq_bank_recon_line_no"),
                models.UniqueConstraint(condition=models.Q(("matched_transaction__isnull", False)), fields=("reconciliation", "matched_transaction"), name="uq_bank_recon_matched_tx"),
            ]},
        ),
    ]
