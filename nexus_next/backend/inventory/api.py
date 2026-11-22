from decimal import Decimal

from django.db.models import F, Q, Sum
from django.utils.dateparse import parse_date
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response

from accounting.models import AccountRole, JournalEntry, JournalLine
from accounting.permissions import CompanyRolePermission
from companies.access import accessible_branches, accessible_companies, can_access_branch, can_access_company, has_company_role, is_platform_owner
from companies.models import Branch, Company, CompanyMembership

from .models import Product, ProductCategory, StockBalance, StockCostLayer, StockMovement, StorageLocation, UnitOfMeasure, Warehouse, WarehouseProductSetting
from .serializers import (
    ProductCategorySerializer, ProductSerializer, StockBalanceSerializer, StockMovementSerializer, StorageLocationSerializer,
    UnitOfMeasureSerializer, WarehouseProductSettingSerializer, WarehouseSerializer,
)


def _default_branch(company_id):
    """تنفيذ وظيفة _default_branch ضمن واجهات api.py مع تطبيق ضوابط البيانات والصلاحيات الخاصة بها."""
    return Branch.objects.filter(company_id=company_id, is_active=True).order_by("-is_head_office", "code").first()


def _can_access_warehouse(user, warehouse):
    """تنفيذ وظيفة _can_access_warehouse ضمن واجهات api.py مع تطبيق ضوابط البيانات والصلاحيات الخاصة بها."""
    if warehouse.branch_id:
        return can_access_branch(user, warehouse.branch_id, warehouse.company_id)
    default_branch = _default_branch(warehouse.company_id)
    return bool(default_branch and can_access_branch(user, default_branch.pk, warehouse.company_id))


def _default_branch_company_ids(user, companies):
    """تنفيذ وظيفة _default_branch_company_ids ضمن واجهات api.py مع تطبيق ضوابط البيانات والصلاحيات الخاصة بها."""
    if user.is_superuser and not is_platform_owner(user):
        return list(companies.values_list("id", flat=True))
    allowed = []
    for company_id in companies.values_list("id", flat=True):
        default_branch = _default_branch(company_id)
        if default_branch and can_access_branch(user, default_branch.pk, company_id):
            allowed.append(company_id)
    return allowed



class CompanyScopedViewSet(viewsets.ModelViewSet):
    permission_classes = (CompanyRolePermission,)
    company_field = "company"

    def _check_company_write(self, company_id):
        """التحقق من صلاحية الكتابة داخل الشركة قبل حفظ التغييرات."""
        if not can_access_company(self.request.user, company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الشركة المحددة.")
        if not has_company_role(self.request.user, company_id, ("admin", "accountant")):
            raise PermissionDenied("التعديل متاح لمدير الشركة والمحاسب فقط.")

    def create(self, request, *args, **kwargs):
        """استقبال طلب إنشاء سجل والتحقق من مدخلاته قبل الحفظ."""
        # Reject unauthorized writes before serializer validation can expose field errors.
        company_id = request.data.get("company")
        if company_id not in (None, ""):
            try:
                self._check_company_write(int(company_id))
            except (TypeError, ValueError):
                pass
        return super().create(request, *args, **kwargs)

    def perform_create(self, serializer):
        """حفظ السجل الجديد بعد تطبيق قواعد الصلاحيات والتحقق من نطاق الشركة."""
        company = serializer.validated_data.get("company")
        if company is None:
            raise PermissionDenied("يجب تحديد الشركة.")
        self._check_company_write(company.pk)
        self._check_related_scope(serializer.validated_data)
        serializer.save()

    def perform_update(self, serializer):
        """حفظ التعديلات مع إعادة التحقق من ثبات الشركة والفرع وصلاحيات الكتابة."""
        company = serializer.validated_data.get("company", serializer.instance.company)
        if company.pk != serializer.instance.company_id:
            raise ValidationError("لا يمكن نقل سجل المخزون إلى شركة أخرى بعد إنشائه.")
        self._check_company_write(company.pk)
        self._check_related_scope(serializer.validated_data)
        serializer.save()

    def _check_related_scope(self, data):
        """التحقق من صلاحية الوصول إلى العلاقات والكيانات المرتبطة بالمستند."""
        return None

    def perform_destroy(self, instance):
        """تنفيذ الحذف المسموح به مع حماية السجلات المرتبطة والمراحل المالية."""
        if not has_company_role(self.request.user, instance.company_id, ("admin",)):
            raise PermissionDenied("حذف البيانات متاح لمدير الشركة فقط.")
        instance.is_active = False
        instance.save(update_fields=("is_active",))


class ProductCategoryViewSet(CompanyScopedViewSet):
    serializer_class = ProductCategorySerializer
    queryset = ProductCategory.objects.select_related("company", "parent")
    filterset_fields = ("company", "parent", "is_active")
    search_fields = ("code", "name_ar")
    ordering_fields = ("code", "name_ar")

    def get_queryset(self):
        """تجهيز مجموعة السجلات المعروضة مع تطبيق نطاق الشركة والفرع وصلاحيات المستخدم."""
        return self.queryset.filter(company__in=accessible_companies(self.request.user))


class UnitOfMeasureViewSet(CompanyScopedViewSet):
    serializer_class = UnitOfMeasureSerializer
    queryset = UnitOfMeasure.objects.select_related("company")
    filterset_fields = ("company", "is_active")
    search_fields = ("code", "name_ar", "symbol")
    ordering_fields = ("code", "name_ar")

    def get_queryset(self):
        """تجهيز مجموعة السجلات المعروضة مع تطبيق نطاق الشركة والفرع وصلاحيات المستخدم."""
        return self.queryset.filter(company__in=accessible_companies(self.request.user))


class ProductViewSet(CompanyScopedViewSet):
    serializer_class = ProductSerializer
    queryset = Product.objects.select_related("company", "category", "unit")
    filterset_fields = ("company", "category", "product_type", "is_active", "track_expiry")
    search_fields = ("code", "barcode", "name_ar", "name_en")
    ordering_fields = ("code", "name_ar", "sale_price", "purchase_price", "created_at")

    def get_queryset(self):
        """تجهيز مجموعة السجلات المعروضة مع تطبيق نطاق الشركة والفرع وصلاحيات المستخدم."""
        return self.queryset.filter(company__in=accessible_companies(self.request.user))


class WarehouseViewSet(CompanyScopedViewSet):
    serializer_class = WarehouseSerializer
    queryset = Warehouse.objects.select_related("company", "branch")
    filterset_fields = ("company", "branch", "is_active")
    search_fields = ("code", "name_ar")
    ordering_fields = ("code", "name_ar", "created_at")

    def get_queryset(self):
        """تجهيز مجموعة السجلات المعروضة مع تطبيق نطاق الشركة والفرع وصلاحيات المستخدم."""
        companies = accessible_companies(self.request.user)
        if self.request.user.is_superuser and not is_platform_owner(self.request.user):
            return self.queryset.filter(company__in=companies)
        branches = accessible_branches(self.request.user)
        default_company_ids = _default_branch_company_ids(self.request.user, companies)
        return self.queryset.filter(company__in=companies).filter(
            Q(branch__in=branches) | Q(branch__isnull=True, company_id__in=default_company_ids)
        )

    def _check_related_scope(self, data):
        """التحقق من صلاحية الوصول إلى العلاقات والكيانات المرتبطة بالمستند."""
        branch = data.get("branch")
        company = data.get("company")
        if branch and not can_access_branch(
            self.request.user, branch.pk, company.pk if company else None
        ):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الفرع المحدد.")
        if company and branch is None and (not self.request.user.is_superuser or is_platform_owner(self.request.user)):
            default_branch = _default_branch(company.pk)
            if not default_branch or not can_access_branch(self.request.user, default_branch.pk, company.pk):
                raise PermissionDenied("المخزن العام يتبع الفرع الرئيسي، ولا تملك صلاحية الوصول إليه.")

    def perform_update(self, serializer):
        """حفظ التعديلات مع إعادة التحقق من ثبات الشركة والفرع وصلاحيات الكتابة."""
        instance = serializer.instance
        company = serializer.validated_data.get("company", instance.company)
        branch = serializer.validated_data.get("branch", instance.branch)
        if company.pk != instance.company_id:
            raise ValidationError("لا يمكن نقل مخزن قائم إلى شركة أخرى.")
        self._check_company_write(company.pk)
        if branch and not can_access_branch(self.request.user, branch.pk, company.pk):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الفرع المحدد.")
        if branch is None and (not self.request.user.is_superuser or is_platform_owner(self.request.user)):
            default_branch = _default_branch(company.pk)
            if not default_branch or not can_access_branch(self.request.user, default_branch.pk, company.pk):
                raise PermissionDenied("المخزن العام يتبع الفرع الرئيسي، ولا تملك صلاحية الوصول إليه.")
        serializer.save()


class WarehouseProductSettingViewSet(viewsets.ModelViewSet):
    serializer_class = WarehouseProductSettingSerializer
    permission_classes = (CompanyRolePermission,)
    queryset = WarehouseProductSetting.objects.select_related("product", "warehouse", "warehouse__company")
    filterset_fields = ("warehouse", "product", "warehouse__company", "is_active")
    search_fields = ("product__code", "product__name_ar", "warehouse__code", "warehouse__name_ar")
    ordering_fields = ("warehouse__code", "product__code", "min_stock", "updated_at")

    def get_queryset(self):
        """تجهيز مجموعة السجلات المعروضة مع تطبيق نطاق الشركة والفرع وصلاحيات المستخدم."""
        companies = accessible_companies(self.request.user)
        queryset = self.queryset.filter(warehouse__company__in=companies)
        if self.request.user.is_superuser and not is_platform_owner(self.request.user):
            return queryset
        branches = accessible_branches(self.request.user)
        default_company_ids = _default_branch_company_ids(self.request.user, companies)
        return queryset.filter(
            Q(warehouse__branch__in=branches)
            | Q(warehouse__branch__isnull=True, warehouse__company_id__in=default_company_ids)
        )

    def _check_write(self, company_id):
        """التحقق من صلاحية تنفيذ عملية تعديل داخل نطاق الشركة."""
        if not can_access_company(self.request.user, company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الشركة المحددة.")
        if not has_company_role(self.request.user, company_id, ("admin", "accountant")):
            raise PermissionDenied("التعديل متاح لمدير الشركة والمحاسب فقط.")

    def perform_create(self, serializer):
        """حفظ السجل الجديد بعد تطبيق قواعد الصلاحيات والتحقق من نطاق الشركة."""
        warehouse = serializer.validated_data["warehouse"]
        self._check_write(warehouse.company_id)
        if not _can_access_warehouse(self.request.user, warehouse):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع المخزن أو الفرع الرئيسي للمخزن العام.")
        serializer.save()

    def perform_update(self, serializer):
        """حفظ التعديلات مع إعادة التحقق من ثبات الشركة والفرع وصلاحيات الكتابة."""
        warehouse = serializer.validated_data.get("warehouse", serializer.instance.warehouse)
        self._check_write(warehouse.company_id)
        if not _can_access_warehouse(self.request.user, warehouse):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع المخزن أو الفرع الرئيسي للمخزن العام.")
        serializer.save()

    def perform_destroy(self, instance):
        """تنفيذ الحذف المسموح به مع حماية السجلات المرتبطة والمراحل المالية."""
        if not has_company_role(self.request.user, instance.warehouse.company_id, ("admin",)):
            raise PermissionDenied("حذف الإعدادات متاح لمدير الشركة فقط.")
        instance.is_active = False
        instance.save(update_fields=("is_active",))


class StorageLocationViewSet(viewsets.ModelViewSet):
    serializer_class = StorageLocationSerializer
    permission_classes = (CompanyRolePermission,)
    queryset = StorageLocation.objects.select_related("warehouse", "warehouse__company", "parent")
    filterset_fields = ("warehouse", "warehouse__company", "location_type", "is_active")
    search_fields = ("code", "name_ar", "warehouse__name_ar")
    ordering_fields = ("code", "name_ar")

    def get_queryset(self):
        """تجهيز مجموعة السجلات المعروضة مع تطبيق نطاق الشركة والفرع وصلاحيات المستخدم."""
        companies = accessible_companies(self.request.user)
        if self.request.user.is_superuser and not is_platform_owner(self.request.user):
            return self.queryset.filter(warehouse__company__in=companies)
        branches = accessible_branches(self.request.user)
        default_company_ids = _default_branch_company_ids(self.request.user, companies)
        return self.queryset.filter(warehouse__company__in=companies).filter(
            Q(warehouse__branch__in=branches)
            | Q(warehouse__branch__isnull=True, warehouse__company_id__in=default_company_ids)
        )

    def create(self, request, *args, **kwargs):
        """استقبال طلب إنشاء سجل والتحقق من مدخلاته قبل الحفظ."""
        warehouse_id = request.data.get("warehouse")
        if warehouse_id not in (None, ""):
            warehouse = Warehouse.objects.filter(pk=warehouse_id).first()
            if warehouse:
                self._check_write(warehouse)
        return super().create(request, *args, **kwargs)

    def perform_destroy(self, instance):
        """تنفيذ الحذف المسموح به مع حماية السجلات المرتبطة والمراحل المالية."""
        self._check_write(instance.warehouse)
        instance.is_active = False
        instance.save(update_fields=("is_active",))

    def _check_write(self, warehouse):
        """التحقق من صلاحية تنفيذ عملية تعديل داخل نطاق الشركة."""
        if not can_access_company(self.request.user, warehouse.company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الشركة المحددة.")
        if not has_company_role(self.request.user, warehouse.company_id, ("admin", "accountant")):
            raise PermissionDenied("التعديل متاح لمدير الشركة والمحاسب فقط.")
        if not _can_access_warehouse(self.request.user, warehouse):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع المخزن أو الفرع الرئيسي للمخزن العام.")

    def perform_create(self, serializer):
        """حفظ السجل الجديد بعد تطبيق قواعد الصلاحيات والتحقق من نطاق الشركة."""
        warehouse = serializer.validated_data["warehouse"]
        self._check_write(warehouse)
        serializer.save()

    def perform_update(self, serializer):
        """حفظ التعديلات مع إعادة التحقق من ثبات الشركة والفرع وصلاحيات الكتابة."""
        warehouse = serializer.validated_data.get("warehouse", serializer.instance.warehouse)
        self._check_write(warehouse)
        serializer.save()


class StockMovementViewSet(viewsets.ModelViewSet):
    serializer_class = StockMovementSerializer
    permission_classes = (CompanyRolePermission,)
    queryset = StockMovement.objects.select_related(
        "company", "source_warehouse", "destination_warehouse", "created_by", "posted_by"
    ).prefetch_related("lines__product", "journal_links__journal_entry__branch")
    filterset_fields = ("company", "movement_type", "status", "movement_date")
    search_fields = ("movement_number", "notes", "lines__product__code", "lines__product__name_ar")
    ordering_fields = ("movement_date", "movement_number", "created_at")

    def get_queryset(self):
        """تجهيز مجموعة السجلات المعروضة مع تطبيق نطاق الشركة والفرع وصلاحيات المستخدم."""
        companies = accessible_companies(self.request.user)
        queryset = self.queryset.filter(company__in=companies)
        if self.request.user.is_superuser and not is_platform_owner(self.request.user):
            return queryset
        branches = accessible_branches(self.request.user)
        default_company_ids = _default_branch_company_ids(self.request.user, companies)
        return queryset.filter(
            Q(source_warehouse__isnull=True) | Q(source_warehouse__branch__in=branches)
            | Q(source_warehouse__branch__isnull=True, source_warehouse__company_id__in=default_company_ids),
            Q(destination_warehouse__isnull=True) | Q(destination_warehouse__branch__in=branches)
            | Q(destination_warehouse__branch__isnull=True, destination_warehouse__company_id__in=default_company_ids),
        ).distinct()

    def create(self, request, *args, **kwargs):
        """استقبال طلب إنشاء سجل والتحقق من مدخلاته قبل الحفظ."""
        company_id = request.data.get("company")
        if company_id not in (None, ""):
            try:
                CompanyScopedViewSet._check_company_write(self, int(company_id))
            except (TypeError, ValueError):
                pass
        return super().create(request, *args, **kwargs)

    def _check_warehouse(self, warehouse, company_id):
        """تنفيذ وظيفة _check_warehouse ضمن واجهات api.py مع تطبيق ضوابط البيانات والصلاحيات الخاصة بها."""
        if not warehouse.is_active:
            raise PermissionDenied("لا يمكن استخدام مخزن موقوف.")
        if warehouse.company_id != company_id:
            raise PermissionDenied("المخزن لا يتبع الشركة المحددة.")
        if not can_access_company(self.request.user, company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الشركة.")
        if not has_company_role(self.request.user, company_id, ("admin", "accountant")):
            raise PermissionDenied("التعديل متاح لمدير الشركة والمحاسب فقط.")
        if not _can_access_warehouse(self.request.user, warehouse):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع المخزن أو الفرع الرئيسي للمخزن العام.")

    def perform_create(self, serializer):
        """حفظ السجل الجديد بعد تطبيق قواعد الصلاحيات والتحقق من نطاق الشركة."""
        company = serializer.validated_data["company"]
        CompanyScopedViewSet._check_company_write(self, company.pk)
        for key in ("source_warehouse", "destination_warehouse"):
            warehouse = serializer.validated_data.get(key)
            if warehouse:
                self._check_warehouse(warehouse, company.pk)
        serializer.save(created_by=self.request.user)

    def perform_update(self, serializer):
        """حفظ التعديلات مع إعادة التحقق من ثبات الشركة والفرع وصلاحيات الكتابة."""
        movement = serializer.instance
        if movement.status != StockMovement.Status.DRAFT:
            raise PermissionDenied("لا يمكن تعديل حركة مرحلة.")
        CompanyScopedViewSet._check_company_write(self, movement.company_id)
        for key in ("source_warehouse", "destination_warehouse"):
            warehouse = serializer.validated_data.get(key, getattr(movement, key))
            if warehouse:
                self._check_warehouse(warehouse, movement.company_id)
        serializer.save()

    def perform_destroy(self, instance):
        """تنفيذ الحذف المسموح به مع حماية السجلات المرتبطة والمراحل المالية."""
        if instance.status != StockMovement.Status.DRAFT:
            raise PermissionDenied("لا يمكن حذف حركة مرحلة.")
        if not has_company_role(self.request.user, instance.company_id, ("admin",)):
            raise PermissionDenied("حذف المسودات متاح لمدير الشركة فقط.")
        instance.delete()

    @action(detail=False, methods=("get",), url_path="stock-card")
    def stock_card(self, request):
        """إعداد بطاقة الصنف مع الرصيد الافتتاحي والحركة والرصيد الجاري."""
        product_id = request.query_params.get("product")
        warehouse_id = request.query_params.get("warehouse")
        if not product_id or not warehouse_id:
            return Response({"detail": "حدد الصنف والمخزن لعرض بطاقة الصنف."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            product_id, warehouse_id = int(product_id), int(warehouse_id)
        except (TypeError, ValueError):
            return Response({"detail": "معرّف الصنف أو المخزن غير صحيح."}, status=status.HTTP_400_BAD_REQUEST)

        companies = accessible_companies(request.user)
        product = Product.objects.filter(pk=product_id, company__in=companies, product_type=Product.ProductType.GOODS).first()
        if product is None:
            raise NotFound("الصنف غير موجود أو غير متاح.")
        warehouse_query = Warehouse.objects.filter(pk=warehouse_id, company_id=product.company_id, is_active=True)
        if not request.user.is_superuser or is_platform_owner(request.user):
            branches = accessible_branches(request.user)
            default_company_ids = _default_branch_company_ids(request.user, companies)
            warehouse_query = warehouse_query.filter(
                Q(branch__in=branches) | Q(branch__isnull=True, company_id__in=default_company_ids)
            )
        warehouse = warehouse_query.first()
        if warehouse is None:
            raise NotFound("المخزن غير موجود أو غير متاح.")

        date_from_raw, date_to_raw = request.query_params.get("date_from"), request.query_params.get("date_to")
        date_from = parse_date(date_from_raw) if date_from_raw else None
        date_to = parse_date(date_to_raw) if date_to_raw else None
        if (date_from_raw and date_from is None) or (date_to_raw and date_to is None):
            return Response({"detail": "صيغة التاريخ يجب أن تكون YYYY-MM-DD."}, status=status.HTTP_400_BAD_REQUEST)
        if date_from and date_to and date_from > date_to:
            return Response({"detail": "تاريخ البداية يجب ألا يتجاوز تاريخ النهاية."}, status=status.HTTP_400_BAD_REQUEST)

        movements = self.get_queryset().filter(
            status=StockMovement.Status.POSTED,
            lines__product_id=product_id,
        ).filter(Q(source_warehouse_id=warehouse_id) | Q(destination_warehouse_id=warehouse_id)).distinct().prefetch_related("lines")
        events = []
        for movement in movements:
            direction = "in" if movement.destination_warehouse_id == warehouse_id else "out"
            for line in movement.lines.all():
                if line.product_id != product_id:
                    continue
                events.append({
                    "date": movement.movement_date,
                    "movement_number": movement.movement_number,
                    "movement_type": movement.movement_type,
                    "direction": direction,
                    "quantity": line.quantity,
                    "unit_cost": line.unit_cost,
                    "cost_amount": line.cost_amount,
                })
        events.sort(key=lambda item: (item["date"], item["movement_number"]))
        opening_quantity, opening_value = Decimal("0"), Decimal("0")
        period_events = []
        for event in events:
            if date_from and event["date"] < date_from:
                sign = Decimal("1") if event["direction"] == "in" else Decimal("-1")
                opening_quantity += sign * event["quantity"]
                opening_value += sign * event["cost_amount"]
            elif date_to and event["date"] > date_to:
                continue
            else:
                period_events.append(event)

        running_quantity, running_value = opening_quantity, opening_value
        rows = []
        for event in period_events:
            incoming = event["quantity"] if event["direction"] == "in" else Decimal("0")
            outgoing = event["quantity"] if event["direction"] == "out" else Decimal("0")
            incoming_value = event["cost_amount"] if event["direction"] == "in" else Decimal("0")
            outgoing_value = event["cost_amount"] if event["direction"] == "out" else Decimal("0")
            running_quantity += incoming - outgoing
            running_value += incoming_value - outgoing_value
            rows.append({
                "date": event["date"].isoformat(),
                "movement_number": event["movement_number"],
                "movement_type": event["movement_type"],
                "direction": event["direction"],
                "quantity_in": str(incoming),
                "quantity_out": str(outgoing),
                "unit_cost": str(event["unit_cost"]),
                "value_in": str(incoming_value),
                "value_out": str(outgoing_value),
                "balance_quantity": str(running_quantity),
                "balance_value": str(running_value),
            })
        return Response({
            "product": product.pk,
            "product_code": product.code,
            "product_name": product.name_ar,
            "warehouse": warehouse.pk,
            "warehouse_name": warehouse.name_ar,
            "date_from": date_from.isoformat() if date_from else None,
            "date_to": date_to.isoformat() if date_to else None,
            "opening_quantity": str(opening_quantity),
            "opening_value": str(opening_value),
            "closing_quantity": str(running_quantity),
            "closing_value": str(running_value),
            "rows": rows,
        })

    @action(detail=True, methods=("post",))
    def post(self, request, pk=None):
        """تنفيذ الإجراء المطلوب بعد التحقق من الصلاحيات وحالة المستند."""
        from .services import post_stock_movement

        movement = self.get_object()
        self._check_warehouse(movement.source_warehouse, movement.company_id) if movement.source_warehouse else None
        self._check_warehouse(movement.destination_warehouse, movement.company_id) if movement.destination_warehouse else None
        movement = post_stock_movement(movement.pk, request.user)
        return Response(self.get_serializer(movement).data, status=status.HTTP_200_OK)



class StockBalanceViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = StockBalanceSerializer
    permission_classes = (CompanyRolePermission,)
    queryset = StockBalance.objects.select_related("product", "warehouse", "warehouse__company")
    filterset_fields = ("warehouse", "product", "warehouse__company", "location", "expiry_date")
    search_fields = ("product__code", "product__name_ar", "warehouse__name_ar")
    ordering_fields = ("product__code", "warehouse__code", "quantity", "inventory_value")

    def get_queryset(self):
        """تجهيز مجموعة السجلات المعروضة مع تطبيق نطاق الشركة والفرع وصلاحيات المستخدم."""
        companies = accessible_companies(self.request.user)
        queryset = self.queryset.filter(warehouse__company__in=companies)
        if self.request.user.is_superuser and not is_platform_owner(self.request.user):
            return queryset
        branches = accessible_branches(self.request.user)
        default_company_ids = _default_branch_company_ids(self.request.user, companies)
        return queryset.filter(
            Q(warehouse__branch__in=branches)
            | Q(warehouse__branch__isnull=True, warehouse__company_id__in=default_company_ids)
        )

    @action(detail=False, methods=("get",), url_path="valuation-integrity")
    def valuation_integrity(self, request):
        """فحص تطابق قيمة المخزون التشغيلي مع الحسابات المحاسبية."""
        company_id = request.query_params.get("company")
        if not company_id:
            return Response({"detail": "حدد الشركة لفحص سلامة تقييم المخزون."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            company_id = int(company_id)
        except (TypeError, ValueError):
            return Response({"detail": "معرّف الشركة غير صحيح."}, status=status.HTTP_400_BAD_REQUEST)
        if not can_access_company(request.user, company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الشركة المحددة.")

        has_all_branch_access = (request.user.is_superuser and not is_platform_owner(request.user)) or CompanyMembership.objects.filter(
            user=request.user, company_id=company_id, is_active=True, all_branches=True
        ).exists()
        if has_all_branch_access:
            branch_ids = list(Branch.objects.filter(company_id=company_id).values_list("id", flat=True))
        else:
            branch_ids = list(accessible_branches(request.user, company_id).values_list("id", flat=True))

        requested_branch = request.query_params.get("branch")
        if requested_branch:
            try:
                requested_branch_id = int(requested_branch)
            except (TypeError, ValueError):
                return Response({"detail": "معرّف الفرع غير صحيح."}, status=status.HTTP_400_BAD_REQUEST)
            selected_branch = Branch.objects.filter(pk=requested_branch_id, company_id=company_id, is_active=True).first()
            if selected_branch is None:
                return Response({"detail": "الفرع المحدد غير موجود أو موقوف."}, status=status.HTTP_400_BAD_REQUEST)
            if selected_branch.pk not in branch_ids:
                raise PermissionDenied("لا تملك صلاحية الوصول إلى الفرع المحدد.")
            branch_ids = [selected_branch.pk]
        else:
            selected_branch = None

        default_branch = Branch.objects.filter(
            company_id=company_id, is_active=True, is_head_office=True
        ).first()
        if default_branch is None:
            default_branch = Branch.objects.filter(company_id=company_id, is_active=True).order_by("code").first()
        include_unassigned = bool(default_branch and default_branch.pk in branch_ids)

        warehouse_scope = Q(warehouse__branch_id__in=branch_ids)
        if include_unassigned:
            warehouse_scope |= Q(warehouse__branch__isnull=True)

        balances = list(
            StockBalance.objects.filter(
                warehouse__company_id=company_id,
                product__costing_method=Product.CostingMethod.FIFO,
            ).filter(warehouse_scope).values(
                "product_id", "warehouse_id", "location_id", "expiry_date", "quantity", "inventory_value",
                "product__code", "product__name_ar", "warehouse__code", "warehouse__name_ar",
            )
        )
        layers = StockCostLayer.objects.filter(
            product__company_id=company_id,
            product__costing_method=Product.CostingMethod.FIFO,
            warehouse__company_id=company_id,
            source_line__movement__status=StockMovement.Status.POSTED,
        ).filter(warehouse_scope).values(
            "product_id", "warehouse_id", "location_id", "expiry_date",
            "product__code", "product__name_ar", "warehouse__code", "warehouse__name_ar",
        ).annotate(
            quantity=Sum("quantity_remaining"),
            value=Sum(F("quantity_remaining") * F("unit_cost")),
        )

        def dimension_key(row):
            """تنفيذ وظيفة dimension_key ضمن واجهات api.py مع تطبيق ضوابط البيانات والصلاحيات الخاصة بها."""
            return (row["product_id"], row["warehouse_id"], row["location_id"], row["expiry_date"])

        balance_map = {dimension_key(row): row for row in balances}
        layer_map = {dimension_key(row): row for row in layers}
        mismatches = []
        for key in set(balance_map) | set(layer_map):
            balance = balance_map.get(key, {})
            layer = layer_map.get(key, {})
            balance_quantity = balance.get("quantity", Decimal("0")) or Decimal("0")
            balance_value = balance.get("inventory_value", Decimal("0")) or Decimal("0")
            layer_quantity = layer.get("quantity", Decimal("0")) or Decimal("0")
            layer_value = layer.get("value", Decimal("0")) or Decimal("0")
            layer_value = layer_value.quantize(Decimal("0.0001"))
            quantity_difference = balance_quantity - layer_quantity
            value_difference = balance_value - layer_value
            if abs(quantity_difference) > Decimal("0.0001") or abs(value_difference) > Decimal("0.0001"):
                mismatches.append({
                    "product": key[0],
                    "product_code": balance.get("product__code") or layer.get("product__code", ""),
                    "product_name": balance.get("product__name_ar") or layer.get("product__name_ar", ""),
                    "warehouse": key[1],
                    "warehouse_code": balance.get("warehouse__code") or layer.get("warehouse__code", ""),
                    "warehouse_name": balance.get("warehouse__name_ar") or layer.get("warehouse__name_ar", ""),
                    "location": key[2],
                    "expiry_date": key[3].isoformat() if key[3] else None,
                    "balance_quantity": str(balance_quantity),
                    "layer_quantity": str(layer_quantity),
                    "quantity_difference": str(quantity_difference),
                    "balance_value": str(balance_value),
                    "layer_value": str(layer_value),
                    "value_difference": str(value_difference),
                })

        source_scope = Q(source_warehouse__isnull=True) | Q(source_warehouse__branch_id__in=branch_ids)
        destination_scope = Q(destination_warehouse__isnull=True) | Q(destination_warehouse__branch_id__in=branch_ids)
        if include_unassigned:
            source_scope |= Q(source_warehouse__branch__isnull=True)
            destination_scope |= Q(destination_warehouse__branch__isnull=True)
        pending = StockMovement.objects.filter(
            company_id=company_id, status=StockMovement.Status.DRAFT
        ).filter(source_scope, destination_scope).select_related(
            "source_warehouse", "destination_warehouse", "created_by"
        ).prefetch_related("lines").order_by("movement_date", "id")
        pending_count = pending.count()
        pending_rows = [{
            "id": movement.pk,
            "movement_number": movement.movement_number,
            "movement_type": movement.movement_type,
            "movement_date": movement.movement_date.isoformat(),
            "created_at": movement.created_at.isoformat(),
            "source_warehouse": movement.source_warehouse.name_ar if movement.source_warehouse_id else "",
            "destination_warehouse": movement.destination_warehouse.name_ar if movement.destination_warehouse_id else "",
            "lines_count": len(movement.lines.all()),
            "created_by": movement.created_by.get_full_name() or movement.created_by.username if movement.created_by_id else "",
        } for movement in pending[:100]]

        return Response({
            "company": company_id,
            "branch": selected_branch.pk if selected_branch else None,
            "branch_name": selected_branch.name_ar if selected_branch else "",
            "fifo_mismatch_count": len(mismatches),
            "fifo_mismatches": mismatches[:200],
            "pending_movements_count": pending_count,
            "pending_movements": pending_rows,
            "pending_movements_truncated": pending_count > len(pending_rows),
            "scope_branch_ids": branch_ids,
            "scope_note": "يشمل الفروع المسموح بها فقط؛ المخازن غير المرتبطة بفرع تُدرج مع المركز الرئيسي.",
        })

    @action(detail=False, methods=("get",), url_path="reconciliation")
    def reconciliation(self, request):
        """إعداد تقرير مطابقة المخزون مع دفتر الأستاذ وإظهار الفروق."""
        company_id = request.query_params.get("company")
        if not company_id:
            return Response({"detail": "حدد الشركة لإجراء مطابقة المخزون."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            company_id = int(company_id)
        except (TypeError, ValueError):
            return Response({"detail": "معرّف الشركة غير صحيح."}, status=status.HTTP_400_BAD_REQUEST)
        if not can_access_company(request.user, company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الشركة المحددة.")

        role = AccountRole.objects.select_related("account").filter(
            company_id=company_id, role=AccountRole.Role.INVENTORY
        ).first()
        if role is None:
            return Response({"detail": "لم يتم ربط دور حساب المخزون بدليل الحسابات."}, status=status.HTTP_400_BAD_REQUEST)
        inventory_account = role.account
        if (
            not inventory_account.is_active or inventory_account.is_group
            or not inventory_account.allow_posting
            or inventory_account.nature != inventory_account.Nature.ASSET
        ):
            return Response({"detail": "حساب المخزون المرتبط يجب أن يكون حساب أصول تفصيليًا ونشطًا وقابلًا للترحيل."}, status=status.HTTP_400_BAD_REQUEST)

        # Never expose company-wide totals to a user who can access only selected branches.
        has_all_branch_access = (request.user.is_superuser and not is_platform_owner(request.user)) or CompanyMembership.objects.filter(
            user=request.user, company_id=company_id, is_active=True, all_branches=True
        ).exists()
        if has_all_branch_access:
            # Inactive branches remain part of consolidated accounting balances.
            accessible_ids = list(
                Branch.objects.filter(company_id=company_id).values_list("id", flat=True)
            )
        else:
            accessible_ids = list(
                accessible_branches(request.user, company_id).values_list("id", flat=True)
            )
        requested_branch = request.query_params.get("branch")
        if requested_branch:
            try:
                requested_branch_id = int(requested_branch)
            except (TypeError, ValueError):
                return Response({"detail": "معرّف الفرع غير صحيح."}, status=status.HTTP_400_BAD_REQUEST)
            selected_branch = Branch.objects.filter(
                pk=requested_branch_id, company_id=company_id, is_active=True
            ).first()
            if selected_branch is None:
                return Response({"detail": "الفرع المحدد غير موجود أو موقوف."}, status=status.HTTP_400_BAD_REQUEST)
            if selected_branch.pk not in accessible_ids:
                raise PermissionDenied("لا تملك صلاحية الوصول إلى الفرع المحدد.")
            branch_ids = [selected_branch.pk]
        else:
            selected_branch = None
            branch_ids = accessible_ids

        default_branch = Branch.objects.filter(
            company_id=company_id, is_active=True, is_head_office=True
        ).first()
        if default_branch is None:
            default_branch = Branch.objects.filter(
                company_id=company_id, is_active=True
            ).order_by("code").first()

        stock_scope = Q(warehouse__branch_id__in=branch_ids)
        # Warehouses without an explicit branch post their inventory journals to
        # the same default branch selected by inventory.posting._branch_for().
        if default_branch and default_branch.pk in branch_ids:
            stock_scope |= Q(warehouse__branch__isnull=True)
        stock_totals = StockBalance.objects.filter(
            warehouse__company_id=company_id
        ).filter(stock_scope).aggregate(total=Sum("inventory_value"))
        stock_value = stock_totals["total"] or Decimal("0")

        gl_totals = JournalLine.objects.filter(
            account_id=inventory_account.pk,
            entry__company_id=company_id,
            entry__branch_id__in=branch_ids,
            entry__status=JournalEntry.Status.POSTED,
        ).aggregate(debit=Sum("debit"), credit=Sum("credit"))
        gl_balance = (gl_totals["debit"] or Decimal("0")) - (gl_totals["credit"] or Decimal("0"))
        difference = gl_balance - stock_value
        currency = Company.objects.select_related("base_currency").get(pk=company_id).base_currency
        # Reconciliation tolerates at most half of the smallest currency unit.
        # The raw difference is still returned so reviewers can inspect rounding.
        rounding_tolerance = Decimal("5").scaleb(-(currency.decimal_places + 1))
        is_reconciled = abs(difference) <= rounding_tolerance
        return Response({
            "company": company_id,
            "currency": currency.code,
            "currency_decimal_places": currency.decimal_places,
            "rounding_tolerance": str(rounding_tolerance),
            "branch": selected_branch.pk if selected_branch else None,
            "branch_code": selected_branch.code if selected_branch else "",
            "branch_name": selected_branch.name_ar if selected_branch else "",
            "scope_branch_ids": branch_ids,
            "scope_note": "عند اختيار كل الفروع، تُحسب الفروع المسموح للمستخدم بالوصول إليها فقط. المخازن غير المرتبطة بفرع تُنسب للمركز الرئيسي، مثل قيود ترحيل المخزون.",
            "account_id": inventory_account.pk,
            "account_code": inventory_account.code,
            "account_name": inventory_account.name_ar,
            "stock_value": str(stock_value),
            "gl_balance": str(gl_balance),
            "difference": str(difference),
            "is_reconciled": is_reconciled,
            "reconciliation_status": "matched" if is_reconciled else "difference",
            "basis": "current_stock_snapshot_vs_posted_inventory_account_balance",
        })
