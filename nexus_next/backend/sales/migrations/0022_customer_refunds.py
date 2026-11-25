import django.core.validators
from decimal import Decimal
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("sales", "0021_customer_receipt_batches"),
        ("treasury", "0009_treasury_fx_revaluation_reversal"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="CustomerRefund",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("refund_number", models.CharField(max_length=40)),
                ("refund_date", models.DateField()),
                ("amount", models.DecimalField(
                    max_digits=20, decimal_places=4,
                    validators=[django.core.validators.MinValueValidator(Decimal("0.0001"))],
                )),
                ("reference", models.CharField(blank=True, max_length=120)),
                ("status", models.CharField(
                    choices=[("draft", "مسودة"), ("posted", "مرحّل"), ("reversed", "معكوس")],
                    default="draft", max_length=10,
                )),
                ("reversal_date", models.DateField(blank=True, null=True)),
                ("reversed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("posted_at", models.DateTimeField(blank=True, null=True)),
                ("branch", models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="customer_refunds", to="companies.branch",
                )),
                ("company", models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="customer_refunds", to="companies.company",
                )),
                ("created_by", models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                    related_name="created_customer_refunds", to=settings.AUTH_USER_MODEL,
                )),
                ("customer", models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="refunds", to="customers.customer",
                )),
                ("reversal_journal_entry", models.OneToOneField(
                    blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                    related_name="customer_refund_reversal", to="accounting.journalentry",
                )),
                ("reversed_by", models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                    related_name="reversed_customer_refunds", to=settings.AUTH_USER_MODEL,
                )),
                ("source_receipt_batch", models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                    related_name="refunds", to="sales.customerreceiptbatch",
                )),
                ("source_return", models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                    related_name="refunds", to="sales.salesreturn",
                )),
                ("treasury_account", models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="customer_refunds", to="treasury.treasuryaccount",
                )),
                ("treasury_transaction", models.OneToOneField(
                    blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                    related_name="customer_refund", to="treasury.treasurytransaction",
                )),
                ("reversal_treasury_transaction", models.OneToOneField(
                    blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                    related_name="customer_refund_reversal", to="treasury.treasurytransaction",
                )),
            ],
            options={"ordering": ("-refund_date", "-id")},
        ),
        migrations.AddConstraint(
            model_name="customerrefund",
            constraint=models.UniqueConstraint(
                fields=("company", "refund_number"), name="uq_sales_refund_company_no",
            ),
        ),
        migrations.AddConstraint(
            model_name="customerrefund",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(source_return__isnull=False, source_receipt_batch__isnull=True)
                    | models.Q(source_return__isnull=True, source_receipt_batch__isnull=False)
                ),
                name="ck_sales_refund_one_source",
            ),
        ),
        migrations.AddIndex(
            model_name="customerrefund",
            index=models.Index(fields=["company", "status", "refund_date"], name="sales_refund_co_stat_idx"),
        ),
        migrations.AddIndex(
            model_name="customerrefund",
            index=models.Index(fields=["customer", "refund_date"], name="sales_refund_cust_date_idx"),
        ),
    ]
