import django.core.validators
from decimal import Decimal
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("sales", "0020_sales_return_document_template"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="CustomerReceiptBatch",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("receipt_number", models.CharField(max_length=40)),
                ("receipt_date", models.DateField()),
                ("method", models.CharField(choices=[("cash", "نقدي"), ("bank", "تحويل بنكي")], default="bank", max_length=8)),
                ("amount", models.DecimalField(decimal_places=4, max_digits=20, validators=[django.core.validators.MinValueValidator(Decimal("0.0001"))])),
                ("reference", models.CharField(blank=True, max_length=120)),
                ("status", models.CharField(choices=[("draft", "مسودة"), ("posted", "مرحّل"), ("reversed", "معكوس")], default="draft", max_length=10)),
                ("reversal_date", models.DateField(blank=True, null=True)),
                ("reversed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("posted_at", models.DateTimeField(blank=True, null=True)),
                ("branch", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="customer_receipt_batches", to="companies.branch")),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="customer_receipt_batches", to="companies.company")),
                ("customer", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="receipt_batches", to="customers.customer")),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="created_customer_receipt_batches", to=settings.AUTH_USER_MODEL)),
                ("journal_entry", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="customer_receipt_batch", to="accounting.journalentry")),
                ("payment_account", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="customer_receipt_batches", to="accounting.chartofaccount")),
                ("reversal_journal_entry", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="reversed_customer_receipt_batch", to="accounting.journalentry")),
                ("reversed_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="reversed_customer_receipt_batches", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ("-receipt_date", "-id")},
        ),
        migrations.AddConstraint(
            model_name="customerreceiptbatch",
            constraint=models.UniqueConstraint(fields=("company", "receipt_number"), name="uq_rcpt_batch_company_no"),
        ),
        migrations.AddIndex(
            model_name="customerreceiptbatch",
            index=models.Index(fields=["company", "status", "receipt_date"], name="sales_rcpt_batch_co_stat_idx"),
        ),
        migrations.AddIndex(
            model_name="customerreceiptbatch",
            index=models.Index(fields=["branch", "receipt_date"], name="sales_rcpt_batch_branch_idx"),
        ),
        migrations.CreateModel(
            name="CustomerReceiptBatchAllocation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("amount", models.DecimalField(decimal_places=4, max_digits=20, validators=[django.core.validators.MinValueValidator(0.0001)])),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("batch", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="allocations", to="sales.customerreceiptbatch")),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="created_customer_receipt_batch_allocations", to=settings.AUTH_USER_MODEL)),
                ("invoice", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="receipt_batch_allocations", to="sales.salesinvoice")),
            ],
            options={"ordering": ("created_at", "id")},
        ),
        migrations.AddIndex(
            model_name="customerreceiptbatchallocation",
            index=models.Index(fields=["invoice", "batch"], name="sales_rcpt_alloc_invoice_idx"),
        ),
    ]
