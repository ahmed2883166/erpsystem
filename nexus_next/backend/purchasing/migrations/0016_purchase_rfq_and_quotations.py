import django.core.validators
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from decimal import Decimal


class Migration(migrations.Migration):
    dependencies = [
        ("purchasing", "0015_purchase_requisition"),
        ("companies", "0001_initial"),
        ("inventory", "0001_initial"),
        ("suppliers", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="PurchaseRFQ",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("rfq_number", models.CharField(max_length=40)),
                ("issue_date", models.DateField()),
                ("closing_date", models.DateField()),
                ("status", models.CharField(choices=[("draft", "مسودة"), ("open", "مفتوح لاستقبال العروض"), ("closed", "مغلق"), ("awarded", "تمت الترسية"), ("cancelled", "ملغي")], default="draft", max_length=12)),
                ("terms", models.TextField(blank=True)),
                ("award_reason", models.TextField(blank=True)),
                ("awarded_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="purchase_rfqs", to="companies.company")),
                ("source_requisition", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="rfqs", to="purchasing.purchaserequisition")),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="created_purchase_rfqs", to=settings.AUTH_USER_MODEL)),
                ("awarded_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="awarded_purchase_rfqs", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ("-issue_date", "-id")},
        ),
        migrations.CreateModel(
            name="PurchaseRFQLine",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("quantity", models.DecimalField(decimal_places=4, max_digits=18, validators=[django.core.validators.MinValueValidator(Decimal("0.0001"))])),
                ("notes", models.CharField(blank=True, max_length=250)),
                ("product", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="purchase_rfq_lines", to="inventory.product")),
                ("rfq", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="lines", to="purchasing.purchaserfq")),
            ],
            options={"ordering": ("id",)},
        ),
        migrations.CreateModel(
            name="PurchaseRFQInvitation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("status", models.CharField(choices=[("invited", "مدعو"), ("declined", "اعتذر"), ("quoted", "قدم عرضًا")], default="invited", max_length=10)),
                ("invited_at", models.DateTimeField(auto_now_add=True)),
                ("rfq", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="invitations", to="purchasing.purchaserfq")),
                ("supplier", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="purchase_rfq_invitations", to="suppliers.supplier")),
            ],
        ),
        migrations.CreateModel(
            name="SupplierQuotation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("quotation_number", models.CharField(max_length=60)),
                ("quotation_date", models.DateField()),
                ("valid_until", models.DateField()),
                ("delivery_days", models.PositiveSmallIntegerField(default=0)),
                ("payment_terms", models.CharField(blank=True, max_length=160)),
                ("notes", models.TextField(blank=True)),
                ("status", models.CharField(choices=[("submitted", "مقدم"), ("withdrawn", "مسحوب")], default="submitted", max_length=12)),
                ("submitted_at", models.DateTimeField(auto_now_add=True)),
                ("rfq", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="quotations", to="purchasing.purchaserfq")),
                ("supplier", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="purchase_quotations", to="suppliers.supplier")),
                ("submitted_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="submitted_supplier_quotations", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ("quotation_date", "id")},
        ),
        migrations.CreateModel(
            name="SupplierQuotationLine",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("unit_price", models.DecimalField(decimal_places=4, max_digits=18, validators=[django.core.validators.MinValueValidator(Decimal("0"))])),
                ("discount_percent", models.DecimalField(decimal_places=4, default=0, max_digits=7, validators=[django.core.validators.MinValueValidator(Decimal("0")), django.core.validators.MaxValueValidator(100)])),
                ("tax_rate", models.DecimalField(decimal_places=4, default=0, max_digits=7, validators=[django.core.validators.MinValueValidator(Decimal("0")), django.core.validators.MaxValueValidator(100)])),
                ("quotation", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="lines", to="purchasing.supplierquotation")),
                ("rfq_line", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="quotation_lines", to="purchasing.purchaserfqline")),
            ],
            options={"ordering": ("id",)},
        ),
        migrations.AddField(
            model_name="purchaserfq",
            name="awarded_quotation",
            field=models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="awarded_rfq", to="purchasing.supplierquotation"),
        ),
        migrations.AddConstraint(
            model_name="purchaserfq",
            constraint=models.UniqueConstraint(fields=("company", "rfq_number"), name="uq_purchase_rfq_company_no"),
        ),
        migrations.AddConstraint(
            model_name="purchaserfqline",
            constraint=models.UniqueConstraint(fields=("rfq", "product"), name="uq_purchase_rfq_product"),
        ),
        migrations.AddIndex(
            model_name="purchaserfq",
            index=models.Index(fields=["company", "status", "closing_date"], name="pur_rfq_co_status_close_idx"),
        ),
        migrations.AddConstraint(
            model_name="purchaserfqinvitation",
            constraint=models.UniqueConstraint(fields=("rfq", "supplier"), name="uq_purchase_rfq_supplier"),
        ),
        migrations.AddConstraint(
            model_name="supplierquotation",
            constraint=models.UniqueConstraint(fields=("rfq", "supplier"), name="uq_supplier_quote_rfq_supplier"),
        ),
        migrations.AddConstraint(
            model_name="supplierquotation",
            constraint=models.UniqueConstraint(fields=("supplier", "quotation_number"), name="uq_supplier_quote_supplier_no"),
        ),
        migrations.AddConstraint(
            model_name="supplierquotationline",
            constraint=models.UniqueConstraint(fields=("quotation", "rfq_line"), name="uq_supplier_quote_line"),
        ),
    ]
