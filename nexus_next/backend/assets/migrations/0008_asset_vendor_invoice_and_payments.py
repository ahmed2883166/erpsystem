import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0001_initial"),
        ("accounting", "0001_initial"),
        ("assets", "0007_asset_classification_and_custody"),
        ("companies", "0001_initial"),
        ("suppliers", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="AssetPurchaseInvoice",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("invoice_number", models.CharField(max_length=80)),
                ("invoice_date", models.DateField()),
                ("net_amount", models.DecimalField(decimal_places=4, max_digits=20)),
                ("tax_amount", models.DecimalField(decimal_places=4, default=0, max_digits=20)),
                ("recoverable_tax_amount", models.DecimalField(decimal_places=4, default=0, max_digits=20)),
                ("nonrecoverable_tax_amount", models.DecimalField(decimal_places=4, default=0, max_digits=20)),
                ("grand_total", models.DecimalField(decimal_places=4, max_digits=20)),
                ("status", models.CharField(choices=[("draft", "مسودة"), ("posted", "مرحّلة")], default="draft", max_length=10)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("posted_at", models.DateTimeField(blank=True, null=True)),
                ("asset", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="vendor_invoice", to="assets.fixedasset")),
                ("branch", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="asset_purchase_invoices", to="companies.branch")),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="asset_purchase_invoices", to="companies.company")),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="created_asset_purchase_invoices", to="accounts.user")),
                ("fiscal_year", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="asset_purchase_invoices", to="companies.fiscalyear")),
                ("journal_entry", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="asset_purchase_invoice", to="accounting.journalentry")),
                ("supplier", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="asset_purchase_invoices", to="suppliers.supplier")),
                ("tax_code", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="asset_purchase_invoices", to="accounting.taxcode")),
            ],
            options={"ordering": ("-invoice_date", "-id")},
        ),
        migrations.AddConstraint(
            model_name="assetpurchaseinvoice",
            constraint=models.UniqueConstraint(fields=("company", "invoice_number"), name="uq_asset_invoice_company_no"),
        ),
        migrations.AddConstraint(
            model_name="assetpurchaseinvoice",
            constraint=models.CheckConstraint(condition=models.Q(("net_amount__gt", 0)), name="ck_asset_invoice_net_positive"),
        ),
        migrations.AddConstraint(
            model_name="assetpurchaseinvoice",
            constraint=models.CheckConstraint(condition=models.Q(("tax_amount__gte", 0), ("recoverable_tax_amount__gte", 0), ("nonrecoverable_tax_amount__gte", 0)), name="ck_asset_invoice_tax_nonnegative"),
        ),
        migrations.AddConstraint(
            model_name="assetpurchaseinvoice",
            constraint=models.CheckConstraint(condition=models.Q(("grand_total__gt", 0)), name="ck_asset_invoice_total_positive"),
        ),
        migrations.AddIndex(
            model_name="assetpurchaseinvoice",
            index=models.Index(fields=["company", "supplier", "status", "invoice_date"], name="asset_inv_supplier_idx"),
        ),
        migrations.CreateModel(
            name="AssetSupplierPayment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("payment_number", models.CharField(max_length=40)),
                ("payment_date", models.DateField()),
                ("amount", models.DecimalField(decimal_places=4, max_digits=20)),
                ("reference", models.CharField(blank=True, max_length=120)),
                ("status", models.CharField(choices=[("draft", "مسودة"), ("posted", "مرحّل"), ("reversed", "معكوس")], default="draft", max_length=10)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("posted_at", models.DateTimeField(blank=True, null=True)),
                ("branch", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="asset_supplier_payments", to="companies.branch")),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="asset_supplier_payments", to="companies.company")),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="created_asset_supplier_payments", to="accounts.user")),
                ("invoice", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="payments", to="assets.assetpurchaseinvoice")),
                ("journal_entry", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="asset_supplier_payment", to="accounting.journalentry")),
                ("payment_account", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="asset_supplier_payments", to="accounting.chartofaccount")),
                ("supplier", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="asset_payments", to="suppliers.supplier")),
            ],
            options={"ordering": ("-payment_date", "-id")},
        ),
        migrations.AddConstraint(
            model_name="assetsupplierpayment",
            constraint=models.UniqueConstraint(fields=("company", "payment_number"), name="uq_asset_payment_company_no"),
        ),
        migrations.AddConstraint(
            model_name="assetsupplierpayment",
            constraint=models.CheckConstraint(condition=models.Q(("amount__gt", 0)), name="ck_asset_payment_amount_positive"),
        ),
        migrations.AddIndex(
            model_name="assetsupplierpayment",
            index=models.Index(fields=["company", "supplier", "status", "payment_date"], name="asset_pay_supplier_idx"),
        ),
    ]
