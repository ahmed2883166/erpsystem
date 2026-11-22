import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("accounting", "0008_account_role_fx_roles"),
        ("accounts", "0001_initial"),
        ("companies", "0003_companymembership_branch_scope"),
        ("purchasing", "0006_purchase_fx_snapshots"),
        ("sales", "0007_sales_fx_snapshots"),
    ]

    operations = [
        migrations.CreateModel(
            name="FxSubledgerRevaluation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("revaluation_date", models.DateField()),
                ("rate_date", models.DateField()),
                ("rate", models.DecimalField(decimal_places=10, max_digits=20)),
                ("foreign_balance", models.DecimalField(decimal_places=4, max_digits=20)),
                ("previous_carrying_amount", models.DecimalField(decimal_places=4, max_digits=20)),
                ("new_carrying_amount", models.DecimalField(decimal_places=4, max_digits=20)),
                ("difference", models.DecimalField(decimal_places=4, max_digits=20)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="fx_subledger_revaluations", to="companies.company")),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="created_fx_subledger_revaluations", to="accounts.user")),
                ("journal_entry", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="fx_subledger_revaluation", to="accounting.journalentry")),
                ("purchase_invoice", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="fx_revaluations", to="purchasing.purchaseinvoice")),
                ("sales_invoice", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="fx_revaluations", to="sales.salesinvoice")),
            ],
            options={"ordering": ("-revaluation_date", "-id")},
        ),
        migrations.AddConstraint(
            model_name="fxsubledgerrevaluation",
            constraint=models.CheckConstraint(
                condition=models.Q(models.Q(("purchase_invoice__isnull", True), ("sales_invoice__isnull", False)), models.Q(("purchase_invoice__isnull", False), ("sales_invoice__isnull", True)), _connector="OR"),
                name="ck_fx_subledger_one_invoice",
            ),
        ),
        migrations.AddConstraint(
            model_name="fxsubledgerrevaluation",
            constraint=models.UniqueConstraint(condition=models.Q(("sales_invoice__isnull", False)), fields=("sales_invoice", "revaluation_date"), name="uq_fx_ar_revaluation_date"),
        ),
        migrations.AddConstraint(
            model_name="fxsubledgerrevaluation",
            constraint=models.UniqueConstraint(condition=models.Q(("purchase_invoice__isnull", False)), fields=("purchase_invoice", "revaluation_date"), name="uq_fx_ap_revaluation_date"),
        ),
        migrations.AddConstraint(
            model_name="fxsubledgerrevaluation",
            constraint=models.CheckConstraint(condition=models.Q(("rate__gt", 0)), name="ck_fx_subledger_rate_positive"),
        ),
        migrations.AddConstraint(
            model_name="fxsubledgerrevaluation",
            constraint=models.CheckConstraint(condition=models.Q(("foreign_balance__gte", 0)), name="ck_fx_subledger_foreign_nonnegative"),
        ),
        migrations.AddConstraint(
            model_name="fxsubledgerrevaluation",
            constraint=models.CheckConstraint(condition=models.Q(("previous_carrying_amount__gte", 0)), name="ck_fx_subledger_previous_nonnegative"),
        ),
        migrations.AddConstraint(
            model_name="fxsubledgerrevaluation",
            constraint=models.CheckConstraint(condition=models.Q(("new_carrying_amount__gte", 0)), name="ck_fx_subledger_new_nonnegative"),
        ),
        migrations.AddIndex(
            model_name="fxsubledgerrevaluation",
            index=models.Index(fields=["company", "revaluation_date"], name="fx_subledger_co_date_idx"),
        ),
    ]
