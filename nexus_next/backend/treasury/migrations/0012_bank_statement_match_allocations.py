from decimal import Decimal

from django.db import migrations, models
import django.db.models.deletion


def migrate_legacy_matches(apps, schema_editor):
    BankStatementLine = apps.get_model("treasury", "BankStatementLine")
    Allocation = apps.get_model("treasury", "BankStatementMatchAllocation")
    for line in BankStatementLine.objects.select_related(
        "reconciliation__bank_account", "matched_transaction"
    ).filter(matched_transaction__isnull=False).iterator():
        doc = line.matched_transaction
        bank = line.reconciliation.bank_account
        foreign = bank.currency_code != bank.company.base_currency.code
        value = doc.transaction_amount if foreign else doc.amount
        if value is None:
            continue
        Allocation.objects.get_or_create(
            line_id=line.pk, transaction_id=doc.pk,
            defaults={"amount": abs(Decimal(value))},
        )


class Migration(migrations.Migration):
    dependencies = [
        ("treasury", "0011_bank_statement_adjustment"),
    ]

    operations = [
        migrations.CreateModel(
            name="BankStatementMatchAllocation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("amount", models.DecimalField(decimal_places=4, max_digits=20)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("line", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="allocations", to="treasury.bankstatementline")),
                ("transaction", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="reconciliation_allocations", to="treasury.treasurytransaction")),
            ],
            options={"ordering": ("created_at", "id")},
        ),
        migrations.AddConstraint(
            model_name="bankstatementmatchallocation",
            constraint=models.UniqueConstraint(fields=("line", "transaction"), name="uq_bank_line_transaction_allocation"),
        ),
        migrations.AddConstraint(
            model_name="bankstatementmatchallocation",
            constraint=models.CheckConstraint(condition=models.Q(("amount__gt", 0)), name="ck_bank_match_allocation_positive"),
        ),
        migrations.RunPython(migrate_legacy_matches, migrations.RunPython.noop),
    ]
