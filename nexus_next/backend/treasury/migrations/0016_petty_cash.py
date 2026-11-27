from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("treasury", "0015_cash_commitments"),
        ("companies", "0001_initial"),
        ("accounting", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="PettyCashFund",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("code", models.CharField(max_length=32)),
                ("name", models.CharField(max_length=160)),
                ("authorized_amount", models.DecimalField(decimal_places=4, max_digits=20)),
                ("status", models.CharField(choices=[("active", "نشطة"), ("suspended", "موقوفة"), ("closed", "مغلقة")], default="active", max_length=12)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("branch", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="petty_cash_funds", to="companies.branch")),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="petty_cash_funds", to="companies.company")),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="created_petty_cash_funds", to=settings.AUTH_USER_MODEL)),
                ("custodian", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="petty_cash_funds", to=settings.AUTH_USER_MODEL)),
                ("funding_account", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="petty_cash_funding_funds", to="treasury.treasuryaccount")),
                ("petty_cash_account", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="petty_cash_fund", to="treasury.treasuryaccount")),
            ],
            options={"ordering": ("code",)},
        ),
        migrations.CreateModel(
            name="PettyCashSettlement",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("settlement_date", models.DateField()),
                ("total_amount", models.DecimalField(decimal_places=4, max_digits=20)),
                ("replenished_amount", models.DecimalField(decimal_places=4, default=0, max_digits=20)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="petty_cash_settlements", to=settings.AUTH_USER_MODEL)),
                ("fund", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="settlements", to="treasury.pettycashfund")),
                ("journal_entry", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="petty_cash_settlement", to="accounting.journalentry")),
            ],
            options={"ordering": ("-settlement_date", "-id")},
        ),
        migrations.CreateModel(
            name="PettyCashExpense",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("expense_date", models.DateField()),
                ("description", models.CharField(max_length=300)),
                ("amount", models.DecimalField(decimal_places=4, max_digits=20)),
                ("receipt_number", models.CharField(blank=True, max_length=100)),
                ("vendor_name", models.CharField(blank=True, max_length=180)),
                ("status", models.CharField(choices=[("draft", "مسودة"), ("settled", "تمت التسوية"), ("void", "ملغاة")], default="draft", max_length=12)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="petty_cash_expenses", to=settings.AUTH_USER_MODEL)),
                ("expense_account", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="petty_cash_expenses", to="accounting.chartofaccount")),
                ("fund", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="expenses", to="treasury.pettycashfund")),
                ("settlement", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="expenses", to="treasury.pettycashsettlement")),
            ],
            options={"ordering": ("expense_date", "id")},
        ),
        migrations.CreateModel(
            name="PettyCashTransfer",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("kind", models.CharField(choices=[("initial", "صرف عهدة"), ("replenishment", "تغذية عهدة")], max_length=16)),
                ("amount", models.DecimalField(decimal_places=4, max_digits=20)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="petty_cash_transfers", to=settings.AUTH_USER_MODEL)),
                ("fund", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="transfers", to="treasury.pettycashfund")),
                ("treasury_transaction", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="petty_cash_transfer", to="treasury.treasurytransaction")),
            ],
            options={"ordering": ("created_at", "id")},
        ),
        migrations.CreateModel(
            name="PettyCashCount",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("count_date", models.DateField()),
                ("expected_balance", models.DecimalField(decimal_places=4, max_digits=20)),
                ("actual_balance", models.DecimalField(decimal_places=4, max_digits=20)),
                ("notes", models.CharField(blank=True, max_length=500)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("counted_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="petty_cash_counts", to=settings.AUTH_USER_MODEL)),
                ("fund", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="counts", to="treasury.pettycashfund")),
            ],
            options={"ordering": ("-count_date", "-id")},
        ),
        migrations.AddConstraint(model_name="pettycashfund", constraint=models.UniqueConstraint(fields=("company", "code"), name="uq_petty_cash_fund_company_code")),
        migrations.AddConstraint(model_name="pettycashfund", constraint=models.CheckConstraint(condition=models.Q(("authorized_amount__gt", 0)), name="ck_petty_cash_fund_authorized_positive")),
        migrations.AddConstraint(model_name="pettycashsettlement", constraint=models.CheckConstraint(condition=models.Q(("total_amount__gt", 0)), name="ck_petty_cash_settlement_total_positive")),
        migrations.AddConstraint(model_name="pettycashsettlement", constraint=models.CheckConstraint(condition=models.Q(("replenished_amount__gte", 0)), name="ck_petty_cash_settlement_replenished_nonnegative")),
        migrations.AddConstraint(model_name="pettycashexpense", constraint=models.CheckConstraint(condition=models.Q(("amount__gt", 0)), name="ck_petty_cash_expense_amount_positive")),
        migrations.AddIndex(model_name="pettycashexpense", index=models.Index(fields=["fund", "status", "expense_date"], name="petty_cash_exp_status_idx")),
        migrations.AddConstraint(model_name="pettycashtransfer", constraint=models.CheckConstraint(condition=models.Q(("amount__gt", 0)), name="ck_petty_cash_transfer_amount_positive")),
        migrations.AddConstraint(model_name="pettycashcount", constraint=models.CheckConstraint(condition=models.Q(("expected_balance__gte", 0)), name="ck_petty_cash_count_expected_nonnegative")),
        migrations.AddConstraint(model_name="pettycashcount", constraint=models.CheckConstraint(condition=models.Q(("actual_balance__gte", 0)), name="ck_petty_cash_count_actual_nonnegative")),
    ]
