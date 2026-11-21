import django.core.validators
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [("companies", "0003_companymembership_branch_scope")]

    operations = [
        migrations.CreateModel(
            name="ProductCategory",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("code", models.CharField(max_length=40)),
                ("name_ar", models.CharField(max_length=160)),
                ("is_active", models.BooleanField(default=True)),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="product_categories", to="companies.company")),
                ("parent", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="children", to="inventory.productcategory")),
            ],
            options={"verbose_name": "مجموعة صنفية", "verbose_name_plural": "المجموعات الصنفية", "ordering": ("code",),
                     "constraints": [models.UniqueConstraint(fields=("company", "code"), name="uq_product_category_company_code")]},
        ),
        migrations.CreateModel(
            name="UnitOfMeasure",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("code", models.CharField(max_length=24)),
                ("name_ar", models.CharField(max_length=80)),
                ("symbol", models.CharField(blank=True, max_length=16)),
                ("decimal_places", models.PositiveSmallIntegerField(default=2)),
                ("is_active", models.BooleanField(default=True)),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="inventory_units", to="companies.company")),
            ],
            options={"verbose_name": "وحدة قياس", "verbose_name_plural": "وحدات القياس", "ordering": ("code",),
                     "constraints": [models.UniqueConstraint(fields=("company", "code"), name="uq_inventory_unit_company_code"),
                                     models.CheckConstraint(condition=models.Q(("decimal_places__lte", 6)), name="ck_inventory_unit_precision")]},
        ),
        migrations.CreateModel(
            name="Product",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("code", models.CharField(max_length=50)),
                ("barcode", models.CharField(blank=True, max_length=80)),
                ("name_ar", models.CharField(max_length=180)),
                ("name_en", models.CharField(blank=True, max_length=180)),
                ("product_type", models.CharField(choices=[("goods", "صنف مخزني"), ("service", "خدمة")], default="goods", max_length=10)),
                ("purchase_price", models.DecimalField(decimal_places=4, default=0, max_digits=18, validators=[django.core.validators.MinValueValidator(0)])),
                ("sale_price", models.DecimalField(decimal_places=4, default=0, max_digits=18, validators=[django.core.validators.MinValueValidator(0)])),
                ("costing_method", models.CharField(choices=[("weighted_average", "متوسط مرجح"), ("fifo", "الوارد أولاً يصرف أولاً")], default="weighted_average", max_length=20)),
                ("min_stock", models.DecimalField(decimal_places=4, default=0, max_digits=18, validators=[django.core.validators.MinValueValidator(0)])),
                ("track_expiry", models.BooleanField(default=False)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("category", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="products", to="inventory.productcategory")),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="products", to="companies.company")),
                ("unit", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="products", to="inventory.unitofmeasure")),
            ],
            options={"verbose_name": "صنف", "verbose_name_plural": "الأصناف", "ordering": ("code",),
                     "constraints": [models.UniqueConstraint(fields=("company", "code"), name="uq_product_company_code"),
                                     models.UniqueConstraint(condition=~models.Q(("barcode", "")), fields=("company", "barcode"), name="uq_product_company_barcode")],
                     "indexes": [models.Index(fields=["company", "product_type", "is_active"], name="inv_product_type_active_idx"),
                                 models.Index(fields=["company", "name_ar"], name="inv_product_name_idx")]},
        ),
        migrations.CreateModel(
            name="Warehouse",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("code", models.CharField(max_length=40)),
                ("name_ar", models.CharField(max_length=160)),
                ("address", models.TextField(blank=True)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("branch", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="warehouses", to="companies.branch")),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="warehouses", to="companies.company")),
            ],
            options={"verbose_name": "مخزن", "verbose_name_plural": "المخازن", "ordering": ("code",),
                     "constraints": [models.UniqueConstraint(fields=("company", "code"), name="uq_warehouse_company_code")],
                     "indexes": [models.Index(fields=["company", "is_active"], name="inv_warehouse_active_idx")]},
        ),
        migrations.CreateModel(
            name="StorageLocation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("code", models.CharField(max_length=40)),
                ("name_ar", models.CharField(max_length=120)),
                ("location_type", models.CharField(choices=[("floor", "دور"), ("area", "منطقة"), ("rack", "رف"), ("bin", "خانة")], default="area", max_length=8)),
                ("is_active", models.BooleanField(default=True)),
                ("parent", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="children", to="inventory.storagelocation")),
                ("warehouse", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="locations", to="inventory.warehouse")),
            ],
            options={"verbose_name": "موقع تخزين", "verbose_name_plural": "مواقع التخزين", "ordering": ("warehouse__code", "code"),
                     "constraints": [models.UniqueConstraint(fields=("warehouse", "code"), name="uq_storage_location_wh_code")]},
        ),
    ]
