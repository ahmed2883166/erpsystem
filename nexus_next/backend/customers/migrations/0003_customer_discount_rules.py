import django.core.validators
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("customers", "0002_customer_discount_profile"),
        ("inventory", "0012_sales_return_reversal_line"),
    ]

    operations = [
        migrations.CreateModel(
            name="CustomerDiscountRule",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("customer_type", models.CharField(blank=True, choices=[("company", "شركة"), ("individual", "فرد")], max_length=12)),
                ("min_quantity", models.DecimalField(blank=True, decimal_places=4, max_digits=18, null=True, validators=[django.core.validators.MinValueValidator(0.0001)])),
                ("min_invoice_amount", models.DecimalField(blank=True, decimal_places=4, help_text="Minimum invoice amount in the company's base currency.", max_digits=20, null=True, validators=[django.core.validators.MinValueValidator(0)])),
                ("discount_percent", models.DecimalField(decimal_places=4, max_digits=7, validators=[django.core.validators.MinValueValidator(0), django.core.validators.MaxValueValidator(100)])),
                ("priority", models.PositiveSmallIntegerField(default=100)),
                ("effective_from", models.DateField()),
                ("effective_to", models.DateField(blank=True, null=True)),
                ("is_active", models.BooleanField(default=True)),
                ("notes", models.CharField(blank=True, max_length=500)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("category", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="customer_discount_rules", to="inventory.productcategory")),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="customer_discount_rules", to="companies.company")),
                ("customer", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="discount_rules", to="customers.customer")),
                ("product", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="customer_discount_rules", to="inventory.product")),
            ],
            options={"ordering": ("-priority", "-id")},
        ),
        migrations.AddIndex(
            model_name="customerdiscountrule",
            index=models.Index(fields=["company", "is_active", "effective_from"], name="cust_disc_co_active_from_idx"),
        ),
        migrations.AddIndex(
            model_name="customerdiscountrule",
            index=models.Index(fields=["company", "customer", "is_active"], name="cust_disc_co_customer_idx"),
        ),
        migrations.AddConstraint(
            model_name="customerdiscountrule",
            constraint=models.CheckConstraint(condition=models.Q(("effective_to__isnull", True), ("effective_to__gte", models.F("effective_from")), _connector="OR"), name="ck_customer_discount_date_range"),
        ),
        migrations.AddConstraint(
            model_name="customerdiscountrule",
            constraint=models.CheckConstraint(condition=models.Q(("min_quantity__isnull", True), ("min_quantity__gt", 0), _connector="OR"), name="ck_customer_discount_min_qty"),
        ),
    ]
