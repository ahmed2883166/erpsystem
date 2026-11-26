from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("assets", "0011_asset_impairment_reversal"),
        ("hr", "0011_employee_loan_disbursement"),
    ]

    operations = [
        migrations.AddField(
            model_name="fixedasset",
            name="custodian_employee",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="fixed_assets_custodied", to="hr.employee",
            ),
        ),
        migrations.CreateModel(
            name="AssetMovement",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("movement_type", models.CharField(choices=[("branch_transfer", "نقل بين الفروع"), ("custody_change", "تغيير العهدة"), ("location_change", "تغيير الموقع")], max_length=24)),
                ("movement_date", models.DateField()),
                ("from_custodian", models.CharField(blank=True, max_length=180)),
                ("to_custodian", models.CharField(blank=True, max_length=180)),
                ("from_location", models.CharField(blank=True, max_length=180)),
                ("to_location", models.CharField(blank=True, max_length=180)),
                ("document_number", models.CharField(max_length=80)),
                ("notes", models.TextField(blank=True)),
                ("status", models.CharField(choices=[("pending", "بانتظار الاعتماد"), ("approved", "معتمد"), ("rejected", "مرفوض")], default="pending", max_length=10)),
                ("reviewed_at", models.DateTimeField(blank=True, null=True)),
                ("review_note", models.CharField(blank=True, max_length=500)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("asset", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="movements", to="assets.fixedasset")),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="asset_movements", to="companies.company")),
                ("from_branch", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="asset_movements_from", to="companies.branch")),
                ("to_branch", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="asset_movements_to", to="companies.branch")),
                ("from_employee", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="asset_movements_from", to="hr.employee")),
                ("to_employee", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="asset_movements_to", to="hr.employee")),
                ("requested_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="requested_asset_movements", to="accounts.user")),
                ("reviewed_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="reviewed_asset_movements", to="accounts.user")),
            ],
            options={"ordering": ("-movement_date", "-id")},
        ),
        migrations.AddConstraint(
            model_name="assetmovement",
            constraint=models.UniqueConstraint(fields=("company", "document_number"), name="uq_asset_movement_company_doc"),
        ),
        migrations.AddIndex(
            model_name="assetmovement",
            index=models.Index(fields=["company", "movement_date"], name="asset_move_co_date_idx"),
        ),
        migrations.AddIndex(
            model_name="assetmovement",
            index=models.Index(fields=["asset", "movement_date"], name="asset_move_asset_date_idx"),
        ),
    ]
