# Generated for treasury cheque register and lifecycle.
from django.db import migrations, models
import django.db.models.deletion
from django.conf import settings

class Migration(migrations.Migration):
    dependencies = [
        ("treasury", "0012_bank_statement_match_allocations"),
        ("companies", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations = [
        migrations.CreateModel(
            name="TreasuryCheque",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("direction", models.CharField(choices=[("incoming", "وارد"), ("outgoing", "صادر")], max_length=8)),
                ("cheque_number", models.CharField(max_length=80)),
                ("counterparty", models.CharField(max_length=180)),
                ("amount", models.DecimalField(decimal_places=4, max_digits=20)),
                ("issue_date", models.DateField()),
                ("due_date", models.DateField()),
                ("status", models.CharField(choices=[("received", "مستلم"), ("deposited", "مودع بالبنك"), ("under_collection", "تحت التحصيل"), ("issued", "صادر"), ("presented", "مقدم للصرف"), ("cleared", "تم التحصيل/الصرف"), ("bounced", "مرتد"), ("cancelled", "ملغي")], max_length=20)),
                ("deposit_date", models.DateField(blank=True, null=True)),
                ("clearing_date", models.DateField(blank=True, null=True)),
                ("bounce_date", models.DateField(blank=True, null=True)),
                ("notes", models.CharField(blank=True, max_length=500)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("bank_account", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="cheques", to="treasury.treasuryaccount")),
                ("branch", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="treasury_cheques", to="companies.branch")),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="treasury_cheques", to="companies.company")),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="created_treasury_cheques", to=settings.AUTH_USER_MODEL)),
                ("fiscal_year", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="treasury_cheques", to="companies.fiscalyear")),
                ("treasury_transaction", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="cheque", to="treasury.treasurytransaction")),
            ],
            options={"ordering": ("due_date", "id")},
        ),
        migrations.AddConstraint(model_name="treasurycheque", constraint=models.UniqueConstraint(fields=("company", "bank_account", "direction", "cheque_number"), name="uq_treasury_cheque_bank_number")),
        migrations.AddConstraint(model_name="treasurycheque", constraint=models.CheckConstraint(condition=models.Q(("amount__gt", 0)), name="ck_treasury_cheque_amount_positive")),
        migrations.AddIndex(model_name="treasurycheque", index=models.Index(fields=["company", "direction", "status", "due_date"], name="tr_cheque_co_status_due_idx")),
    ]
