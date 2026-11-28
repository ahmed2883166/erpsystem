from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("purchasing", "0027_landed_cost_manual_basis_default"),
        ("companies", "0006_ensure_company_head_office"),
        ("suppliers", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="BranchPurchasePolicy",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("purchase_enabled", models.BooleanField(default=True)),
                ("restrict_suppliers", models.BooleanField(default=False)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("allowed_suppliers", models.ManyToManyField(blank=True, related_name="branch_purchase_policies", to="suppliers.supplier")),
                ("branch", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="purchase_policy", to="companies.branch")),
                ("updated_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="updated_branch_purchase_policies", to=settings.AUTH_USER_MODEL)),
            ],
            options={"verbose_name": "سياسة شراء فرع", "verbose_name_plural": "سياسات شراء الفروع"},
        ),
        migrations.CreateModel(
            name="BranchPurchasePolicyAudit",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("old_values", models.JSONField(default=dict)),
                ("new_values", models.JSONField(default=dict)),
                ("changed_at", models.DateTimeField(auto_now_add=True)),
                ("changed_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="branch_purchase_policy_audits", to=settings.AUTH_USER_MODEL)),
                ("policy", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="audit_entries", to="purchasing.branchpurchasepolicy")),
            ],
            options={"ordering": ("-changed_at", "-pk")},
        ),
        migrations.AddIndex(
            model_name="branchpurchasepolicyaudit",
            index=models.Index(fields=["policy", "-changed_at"], name="branch_buy_audit_idx"),
        ),
    ]
