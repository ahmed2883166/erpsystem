import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True
    dependencies = [
        ("accounting", "0001_initial"),
        ("companies", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations = [
        migrations.CreateModel(
            name="TreasuryAccount",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("kind", models.CharField(choices=[("cash", "خزينة"), ("bank", "بنك")], max_length=8)),
                ("code", models.CharField(max_length=32)),
                ("name_ar", models.CharField(max_length=160)),
                ("bank_name", models.CharField(blank=True, max_length=160)),
                ("account_number", models.CharField(blank=True, max_length=80)),
                ("iban", models.CharField(blank=True, max_length=64)),
                ("currency_code", models.CharField(default="EGP", max_length=3)),
                ("opening_balance", models.DecimalField(decimal_places=4, default=0, max_digits=20)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("account", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="treasury_account", to="accounting.chartofaccount")),
                ("branch", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="treasury_accounts", to="companies.branch")),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="treasury_accounts", to="companies.company")),
            ],
            options={"ordering": ("kind", "code"), "indexes": [models.Index(fields=["company", "kind", "is_active"], name="tr_co_kind_active_idx")], "constraints": [models.UniqueConstraint(fields=("company", "code"), name="uq_treasury_account_company_code")]},
        ),
        migrations.CreateModel(
            name="TreasuryTransaction",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("kind", models.CharField(choices=[("receipt", "سند قبض"), ("payment", "سند صرف"), ("transfer", "تحويل بين الخزائن")], max_length=10)),
                ("status", models.CharField(choices=[("draft", "مسودة"), ("posted", "مرحّل")], default="draft", max_length=8)),
                ("document_number", models.CharField(max_length=40)),
                ("transaction_date", models.DateField()),
                ("amount", models.DecimalField(decimal_places=4, max_digits=20)),
                ("description", models.CharField(max_length=500)),
                ("reference", models.CharField(blank=True, max_length=120)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("posted_at", models.DateTimeField(blank=True, null=True)),
                ("branch", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="treasury_transactions", to="companies.branch")),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="treasury_transactions", to="companies.company")),
                ("counter_account", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="treasury_counter_transactions", to="accounting.chartofaccount")),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="created_treasury_transactions", to=settings.AUTH_USER_MODEL)),
                ("destination_account", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="incoming_transfers", to="treasury.treasuryaccount")),
                ("fiscal_year", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="treasury_transactions", to="companies.fiscalyear")),
                ("journal_entry", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="treasury_transaction", to="accounting.journalentry")),
                ("source_account", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="outgoing_transactions", to="treasury.treasuryaccount")),
            ],
            options={"ordering": ("-transaction_date", "-id"), "indexes": [models.Index(fields=["company", "status", "transaction_date"], name="tr_co_status_date_idx")], "constraints": [models.UniqueConstraint(fields=("company", "document_number"), name="uq_treasury_doc_company_no")]},
        ),
    ]
