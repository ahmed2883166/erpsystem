from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("assets", "0012_asset_transfer_custody"),
        ("accounting", "0001_initial"),
        ("accounts", "0001_initial"),
    ]

    operations = [
        migrations.AlterField(
            model_name="fixedasset",
            name="depreciation_method",
            field=models.CharField(
                choices=[
                    ("straight_line", "القسط الثابت"),
                    ("declining_balance", "القسط المتناقص"),
                    ("units_of_production", "وحدات الإنتاج"),
                ],
                default="straight_line",
                max_length=24,
            ),
        ),
        migrations.AddField(
            model_name="fixedasset",
            name="declining_rate_percentage",
            field=models.DecimalField(blank=True, decimal_places=4, max_digits=7, null=True),
        ),
        migrations.AddField(
            model_name="fixedasset",
            name="production_units_total",
            field=models.PositiveBigIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="assetdepreciation",
            name="units_produced",
            field=models.PositiveBigIntegerField(blank=True, null=True),
        ),
        migrations.CreateModel(
            name="AssetComponent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("code", models.CharField(max_length=40)),
                ("name_ar", models.CharField(max_length=180)),
                ("original_cost", models.DecimalField(decimal_places=4, max_digits=20)),
                ("salvage_value", models.DecimalField(decimal_places=4, default=0, max_digits=20)),
                ("useful_life_months", models.PositiveIntegerField()),
                ("depreciation_method", models.CharField(choices=[("straight_line", "القسط الثابت"), ("declining_balance", "القسط المتناقص"), ("units_of_production", "وحدات الإنتاج")], default="straight_line", max_length=24)),
                ("declining_rate_percentage", models.DecimalField(blank=True, decimal_places=4, max_digits=7, null=True)),
                ("production_units_total", models.PositiveBigIntegerField(blank=True, null=True)),
                ("status", models.CharField(choices=[("active", "نشط"), ("disposed", "مستبعد")], default="active", max_length=10)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("accumulated_depreciation_account", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="fixed_asset_component_accumulated", to="accounting.chartofaccount")),
                ("asset", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="components", to="assets.fixedasset")),
                ("asset_account", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="fixed_asset_component_costs", to="accounting.chartofaccount")),
                ("depreciation_expense_account", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="fixed_asset_component_expenses", to="accounting.chartofaccount")),
            ],
            options={"ordering": ("code",)},
        ),
        migrations.AddConstraint(model_name="assetcomponent", constraint=models.UniqueConstraint(fields=("asset", "code"), name="uq_asset_component_code")),
        migrations.AddConstraint(model_name="assetcomponent", constraint=models.CheckConstraint(condition=models.Q(("original_cost__gt", 0)), name="ck_asset_component_cost_positive")),
        migrations.AddConstraint(model_name="assetcomponent", constraint=models.CheckConstraint(condition=models.Q(("salvage_value__gte", 0)), name="ck_asset_component_salvage_nonnegative")),
        migrations.AddConstraint(model_name="assetcomponent", constraint=models.CheckConstraint(condition=models.Q(("useful_life_months__gt", 0)), name="ck_asset_component_life_positive")),
        migrations.CreateModel(
            name="AssetInsurancePolicy",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("policy_number", models.CharField(max_length=100)),
                ("insurer_name", models.CharField(max_length=180)),
                ("coverage_description", models.TextField(blank=True)),
                ("start_date", models.DateField()),
                ("end_date", models.DateField()),
                ("insured_value", models.DecimalField(decimal_places=4, max_digits=20)),
                ("premium_amount", models.DecimalField(decimal_places=4, default=0, max_digits=20)),
                ("document_reference", models.CharField(blank=True, max_length=500)),
                ("status", models.CharField(choices=[("active", "سارية"), ("expired", "منتهية"), ("cancelled", "ملغاة")], default="active", max_length=10)),
                ("notes", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("asset", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="insurance_policies", to="assets.fixedasset")),
            ],
            options={"ordering": ("-end_date", "-id")},
        ),
        migrations.AddConstraint(model_name="assetinsurancepolicy", constraint=models.UniqueConstraint(fields=("asset", "policy_number"), name="uq_asset_insurance_policy")),
        migrations.AddConstraint(model_name="assetinsurancepolicy", constraint=models.CheckConstraint(condition=models.Q(("end_date__gte", models.F("start_date"))), name="ck_asset_insurance_dates")),
        migrations.AddConstraint(model_name="assetinsurancepolicy", constraint=models.CheckConstraint(condition=models.Q(("insured_value__gte", 0)), name="ck_asset_insured_value_nonnegative")),
        migrations.AddConstraint(model_name="assetinsurancepolicy", constraint=models.CheckConstraint(condition=models.Q(("premium_amount__gte", 0)), name="ck_asset_insurance_premium_nonnegative")),
        migrations.CreateModel(
            name="AssetPhysicalInventory",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("count_date", models.DateField()),
                ("expected_location", models.CharField(blank=True, max_length=180)),
                ("counted_location", models.CharField(blank=True, max_length=180)),
                ("expected_custodian", models.CharField(blank=True, max_length=180)),
                ("counted_custodian", models.CharField(blank=True, max_length=180)),
                ("result", models.CharField(choices=[("matched", "مطابق"), ("location_mismatch", "اختلاف موقع"), ("custodian_mismatch", "اختلاف عهدة"), ("condition_issue", "ملاحظة حالة"), ("not_found", "غير موجود")], max_length=24)),
                ("condition_notes", models.TextField(blank=True)),
                ("notes", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("asset", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="physical_inventory", to="assets.fixedasset")),
                ("counted_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="asset_inventory_counts", to="accounts.user")),
            ],
            options={"ordering": ("-count_date", "-id")},
        ),
        migrations.AddConstraint(model_name="assetphysicalinventory", constraint=models.UniqueConstraint(fields=("asset", "count_date"), name="uq_asset_physical_count_date")),
    ]
