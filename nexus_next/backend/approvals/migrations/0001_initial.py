import django.core.validators
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("companies", "0001_initial"),
        ("sales", "0003_sales_discount_approval_snapshot"),
    ]

    operations = [
        migrations.CreateModel(
            name="DiscountApprovalPolicy",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("max_discount_percent", models.DecimalField(
                    decimal_places=4, max_digits=7,
                    validators=[django.core.validators.MinValueValidator(0), django.core.validators.MaxValueValidator(100)],
                    help_text="الحد الأقصى للخصم المسموح دون موافقة مدير الشركة.",
                )),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("company", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="discount_approval_policy", to="companies.company")),
                ("created_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="created_discount_policies", to=settings.AUTH_USER_MODEL)),
                ("updated_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="updated_discount_policies", to=settings.AUTH_USER_MODEL)),
            ],
            options={"verbose_name": "سياسة حد الخصم", "verbose_name_plural": "سياسات حدود الخصم"},
        ),
        migrations.CreateModel(
            name="SalesInvoiceApproval",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("requested_limit", models.DecimalField(decimal_places=4, max_digits=7)),
                ("status", models.CharField(choices=[("pending", "بانتظار الموافقة"), ("approved", "تمت الموافقة"), ("rejected", "مرفوض")], default="pending", max_length=10)),
                ("decision_note", models.CharField(blank=True, max_length=500)),
                ("requested_at", models.DateTimeField(auto_now_add=True)),
                ("decided_at", models.DateTimeField(blank=True, null=True)),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="sales_invoice_approvals", to="companies.company")),
                ("decided_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="decided_sales_discount_requests", to=settings.AUTH_USER_MODEL)),
                ("invoice", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="approval_request", to="sales.salesinvoice")),
                ("requested_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="sales_discount_requests", to=settings.AUTH_USER_MODEL)),
            ],
            options={"verbose_name": "طلب موافقة خصم", "verbose_name_plural": "طلبات موافقات الخصم", "ordering": ("status", "-requested_at")},
        ),
        migrations.AddIndex(
            model_name="salesinvoiceapproval",
            index=models.Index(fields=["company", "status", "requested_at"], name="approval_co_status_date_idx"),
        ),
    ]
