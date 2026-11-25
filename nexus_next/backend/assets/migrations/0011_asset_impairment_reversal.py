from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("accounting", "0011_ledger_reporting_indexes"),
        ("assets", "0010_asset_impairment"),
    ]

    operations = [
        migrations.CreateModel(
            name="AssetImpairmentReversal",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("reversal_date", models.DateField()),
                ("carrying_amount_before", models.DecimalField(max_digits=20, decimal_places=4)),
                ("recoverable_amount", models.DecimalField(max_digits=20, decimal_places=4)),
                ("hypothetical_carrying_amount", models.DecimalField(max_digits=20, decimal_places=4)),
                ("reversal_amount", models.DecimalField(max_digits=20, decimal_places=4)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("asset", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="impairment_reversals", to="assets.fixedasset")),
                ("reversal_income_account", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="fixed_asset_impairment_reversal_income", to="accounting.chartofaccount")),
                ("journal_entry", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="asset_impairment_reversal", to="accounting.journalentry")),
            ],
            options={"ordering": ("reversal_date", "id")},
        ),
        migrations.AddConstraint(
            model_name="assetimpairmentreversal",
            constraint=models.UniqueConstraint(fields=("asset", "reversal_date"), name="uq_asset_impairment_reversal_date"),
        ),
        migrations.AddConstraint(
            model_name="assetimpairmentreversal",
            constraint=models.CheckConstraint(condition=models.Q(("carrying_amount_before__gte", 0)), name="ck_asset_reversal_carrying_nonnegative"),
        ),
        migrations.AddConstraint(
            model_name="assetimpairmentreversal",
            constraint=models.CheckConstraint(condition=models.Q(("recoverable_amount__gte", 0)), name="ck_asset_reversal_recoverable_nonnegative"),
        ),
        migrations.AddConstraint(
            model_name="assetimpairmentreversal",
            constraint=models.CheckConstraint(condition=models.Q(("hypothetical_carrying_amount__gte", 0)), name="ck_asset_reversal_ceiling_nonnegative"),
        ),
        migrations.AddConstraint(
            model_name="assetimpairmentreversal",
            constraint=models.CheckConstraint(condition=models.Q(("reversal_amount__gt", 0)), name="ck_asset_reversal_amount_positive"),
        ),
        migrations.CreateModel(
            name="AssetImpairmentReversalAllocation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("amount", models.DecimalField(max_digits=20, decimal_places=4)),
                ("impairment", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="reversal_allocations", to="assets.assetimpairment")),
                ("reversal", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="allocations", to="assets.assetimpairmentreversal")),
            ],
        ),
        migrations.AddConstraint(
            model_name="assetimpairmentreversalallocation",
            constraint=models.UniqueConstraint(fields=("reversal", "impairment"), name="uq_asset_reversal_impairment"),
        ),
        migrations.AddConstraint(
            model_name="assetimpairmentreversalallocation",
            constraint=models.CheckConstraint(condition=models.Q(("amount__gt", 0)), name="ck_asset_reversal_allocation_positive"),
        ),
    ]
