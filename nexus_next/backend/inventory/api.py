from datetime import date, timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Case, Count, DateField, DecimalField, ExpressionWrapper, F, OuterRef, Q, Subquery, Sum, Value, When
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response

from accounting.models import AccountRole, JournalEntry, JournalLine
from accounting.permissions import CompanyRolePermission
from companies.access import accessible_branches, accessible_companies, can_access_branch, can_access_company, has_company_role, is_platform_owner
from companies.models import Branch, Company, CompanyMembership

from .models import InventoryCount, InventoryCountLine, Product, ProductCategory, StockBalance, StockCostConsumption, StockCostLayer, StockMovement, StockMovementLine, StorageLocation, UnitOfMeasure, Warehouse, WarehouseProductSetting, WarehouseStatusAudit
from .serializers import (
    ProductCategorySerializer, ProductSerializer, StockBalanceSerializer, StockMovementSerializer, StorageLocationSerializer,
    UnitOfMeasureSerializer, WarehouseProductSettingSerializer, WarehouseSerializer,
    InventoryCountSerializer, InventoryCountLineSerializer,
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
    queryset = Warehouse.objects.select_related("company", "branch").filter(is_transit=False)
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

    @transaction.atomic
    def perform_update(self, serializer):
        warehouse = serializer.instance
        company = serializer.validated_data.get("company", warehouse.company)
        branch = serializer.validated_data.get("branch", warehouse.branch)
        if company.pk != warehouse.company_id:
            raise ValidationError("لا يمكن نقل مخزن قائم إلى شركة أخرى.")
        self._check_company_write(company.pk)
        if branch and not can_access_branch(self.request.user, branch.pk, company.pk):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الفرع المحدد.")
        if branch is None and (not self.request.user.is_superuser or is_platform_owner(self.request.user)):
            default_branch = _default_branch(company.pk)
            if not default_branch or not can_access_branch(self.request.user, default_branch.pk, company.pk):
                raise PermissionDenied("المخزن العام يتبع الفرع الرئيسي، ولا تملك صلاحية الوصول إليه.")
        previous_active = warehouse.is_active
        next_active = serializer.validated_data.get("is_active", previous_active)
        if previous_active != next_active:
            if not has_company_role(self.request.user, company.pk, ("admin",)):
                raise PermissionDenied("إيقاف المخزن أو إعادة فتحه متاح لمدير الشركة فقط.")
            serializer.save()
            WarehouseStatusAudit.objects.create(
                warehouse=warehouse,
                previous_is_active=previous_active,
                new_is_active=next_active,
                reason=str(self.request.data.get("status_change_reason", "")).strip(),
                changed_by=self.request.user,
            )
            return
        serializer.save()

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
        """حفظ التعديلات مع منع نقل موقع التخزين إلى شركة أخرى."""
        current = serializer.instance
        warehouse = serializer.validated_data.get("warehouse", current.warehouse)
        if warehouse.company_id != current.warehouse.company_id:
            raise ValidationError({"warehouse": "لا يمكن نقل موقع تخزين قائم إلى مخزن تابع لشركة أخرى."})
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
        if warehouse.is_transit:
            raise PermissionDenied("مخزن البضاعة بالطريق مخصص لدورة التحويلات ولا يستخدم في الحركات اليدوية.")
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

    @action(detail=False, methods=("get",), url_path="recent")
    def recent(self, request):
        """إرجاع آخر عدد محدود من الحركات بدل تحميل سجل المخزون بالكامل للواجهة."""
        company_id = request.query_params.get("company")
        if not company_id:
            return Response({"detail": "حدد الشركة لعرض الحركات الأخيرة."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            company_id = int(company_id)
            limit = int(request.query_params.get("limit", "100"))
        except (TypeError, ValueError):
            return Response({"detail": "معرّف الشركة أو عدد الحركات غير صحيح."}, status=status.HTTP_400_BAD_REQUEST)
        if not can_access_company(request.user, company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الشركة المحددة.")
        limit = max(1, min(limit, 500))
        queryset = self.filter_queryset(self.get_queryset().filter(company_id=company_id))
        warehouse_id = request.query_params.get("warehouse")
        if warehouse_id:
            try:
                warehouse_id = int(warehouse_id)
            except (TypeError, ValueError):
                return Response({"detail": "معرّف المخزن غير صحيح."}, status=status.HTTP_400_BAD_REQUEST)
            warehouse = Warehouse.objects.filter(
                pk=warehouse_id, company_id=company_id, is_active=True,
            ).first()
            if warehouse is None:
                raise NotFound("المخزن غير موجود أو غير متاح.")
            if not _can_access_warehouse(request.user, warehouse):
                raise PermissionDenied("لا تملك صلاحية الوصول إلى المخزن المحدد.")
            queryset = queryset.filter(
                Q(source_warehouse_id=warehouse_id) | Q(destination_warehouse_id=warehouse_id)
            )
        rows = queryset.order_by("-movement_date", "-created_at", "-id")[:limit]
        return Response(self.get_serializer(rows, many=True).data)

    @action(detail=False, methods=("get",), url_path="pending")
    def pending(self, request):
        """List unposted stock movements for operational follow-up."""
        company_id = request.query_params.get("company")
        if not company_id:
            raise ValidationError({"company": "حدد الشركة لعرض الحركات المعلقة."})
        try:
            company_id = int(company_id)
            limit = max(1, min(int(request.query_params.get("limit", "200")), 500))
        except (TypeError, ValueError):
            raise ValidationError({"detail": "معرّف الشركة أو عدد الحركات غير صحيح."}) from None
        if not can_access_company(request.user, company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الشركة المحددة.")
        queryset = self.filter_queryset(
            self.get_queryset().filter(company_id=company_id, status=StockMovement.Status.DRAFT)
        )
        warehouse_id = request.query_params.get("warehouse")
        if warehouse_id:
            try:
                warehouse_id = int(warehouse_id)
            except (TypeError, ValueError):
                raise ValidationError({"warehouse": "معرّف المخزن غير صحيح."}) from None
            warehouse = Warehouse.objects.filter(pk=warehouse_id, company_id=company_id).first()
            if warehouse is None or not _can_access_warehouse(request.user, warehouse):
                raise NotFound("المخزن غير موجود أو غير متاح.")
            queryset = queryset.filter(
                Q(source_warehouse_id=warehouse_id) | Q(destination_warehouse_id=warehouse_id)
            )
        total = queryset.count()
        rows = queryset.order_by("movement_date", "created_at", "id")[:limit]
        return Response({
            "count": total,
            "truncated": total > limit,
            "results": self.get_serializer(rows, many=True).data,
        })

    @action(detail=False, methods=("get",), url_path="stock-card")
    def stock_card(self, request):
        """إعداد بطاقة الصنف بترقيم صفحات وحساب الأرصدة داخل قاعدة البيانات."""
        product_id = request.query_params.get("product")
        warehouse_id = request.query_params.get("warehouse")
        if not product_id or not warehouse_id:
            return Response({"detail": "حدد الصنف والمخزن لعرض بطاقة الصنف."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            product_id, warehouse_id = int(product_id), int(warehouse_id)
            page = int(request.query_params.get("page", "1"))
            page_size = int(request.query_params.get("page_size", "50"))
        except (TypeError, ValueError):
            return Response({"detail": "معرّف الصنف أو المخزن أو رقم الصفحة غير صحيح."}, status=status.HTTP_400_BAD_REQUEST)
        if page < 1 or page_size < 1 or page_size > 200:
            return Response({"detail": "رقم الصفحة يجب أن يكون موجبًا وحجم الصفحة بين 1 و200."}, status=status.HTTP_400_BAD_REQUEST)

        companies = accessible_companies(request.user)
        product = Product.objects.filter(
            pk=product_id, company__in=companies, product_type=Product.ProductType.GOODS,
        ).first()
        if product is None:
            raise NotFound("الصنف غير موجود أو غير متاح.")
        # Inactive warehouses remain readable for historical stock-card review.
        # The write path still rejects them in _check_warehouse().
        warehouse_query = Warehouse.objects.filter(
            pk=warehouse_id, company_id=product.company_id,
        )
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
            return Response({"detail": "تاريخ البداية يجب ألا يتجاوز النهاية."}, status=status.HTTP_400_BAD_REQUEST)

        base = StockMovementLine.objects.filter(
            product_id=product_id,
            movement__status=StockMovement.Status.POSTED,
        ).filter(
            Q(movement__source_warehouse_id=warehouse_id)
            | Q(movement__destination_warehouse_id=warehouse_id)
        )
        if date_to:
            base = base.filter(movement__movement_date__lte=date_to)

        quantity_in = Case(
            When(movement__destination_warehouse_id=warehouse_id, then=F("quantity")),
            default=Value(0), output_field=DecimalField(max_digits=20, decimal_places=4),
        )
        quantity_out = Case(
            When(movement__source_warehouse_id=warehouse_id, then=F("quantity")),
            default=Value(0), output_field=DecimalField(max_digits=20, decimal_places=4),
        )
        value_in = Case(
            When(movement__destination_warehouse_id=warehouse_id, then=F("cost_amount")),
            default=Value(0), output_field=DecimalField(max_digits=22, decimal_places=4),
        )
        value_out = Case(
            When(movement__source_warehouse_id=warehouse_id, then=F("cost_amount")),
            default=Value(0), output_field=DecimalField(max_digits=22, decimal_places=4),
        )

        def totals(queryset):
            result = queryset.aggregate(
                quantity_in=Sum(quantity_in), quantity_out=Sum(quantity_out),
                value_in=Sum(value_in), value_out=Sum(value_out),
            )
            q_in = result["quantity_in"] or Decimal("0")
            q_out = result["quantity_out"] or Decimal("0")
            v_in = result["value_in"] or Decimal("0")
            v_out = result["value_out"] or Decimal("0")
            return q_in - q_out, v_in - v_out

        opening_quantity, opening_value = (
            totals(base.filter(movement__movement_date__lt=date_from))
            if date_from else (Decimal("0"), Decimal("0"))
        )
        period = base.filter(movement__movement_date__gte=date_from) if date_from else base
        total_rows = period.count()
        total_pages = (total_rows + page_size - 1) // page_size
        ordered = period.annotate(
            _card_posted_order=Coalesce("movement__posted_at", "movement__created_at"),
        ).order_by(
            "movement__movement_date", "_card_posted_order", "movement_id", "id",
        )
        page_rows = list(ordered.values(
            "id", "movement_id", "movement__movement_date", "movement__posted_at",
            "movement__created_at", "movement__movement_number", "movement__movement_type",
            "movement__source_warehouse_id", "movement__destination_warehouse_id", "quantity", "unit_cost", "cost_amount",
            "_card_posted_order",
        )[(page - 1) * page_size:page * page_size])

        if page_rows:
            first = page_rows[0]
            first_date = first["movement__movement_date"]
            first_posted = first["_card_posted_order"]
            first_movement_id = first["movement_id"]
            first_line_id = first["id"]
            before_first = base.annotate(
                _card_posted_order=Coalesce("movement__posted_at", "movement__created_at"),
            ).filter(
                Q(movement__movement_date__lt=first_date)
                | Q(movement__movement_date=first_date, _card_posted_order__lt=first_posted)
                | Q(movement__movement_date=first_date, _card_posted_order=first_posted, movement_id__lt=first_movement_id)
                | Q(movement__movement_date=first_date, _card_posted_order=first_posted, movement_id=first_movement_id, id__lt=first_line_id)
            )
            running_quantity, running_value = totals(before_first)
        else:
            running_quantity, running_value = opening_quantity, opening_value

        rows = []
        for event in page_rows:
            incoming = event["quantity"] if event["movement__destination_warehouse_id"] == warehouse_id else Decimal("0")
            outgoing = event["quantity"] if event["movement__source_warehouse_id"] == warehouse_id else Decimal("0")
            incoming_value = event["cost_amount"] if incoming else Decimal("0")
            outgoing_value = event["cost_amount"] if outgoing else Decimal("0")
            running_quantity += incoming - outgoing
            running_value += incoming_value - outgoing_value
            rows.append({
                "date": event["movement__movement_date"].isoformat(),
                "posted_at": event["movement__posted_at"].isoformat() if event["movement__posted_at"] else None,
                "movement_number": event["movement__movement_number"],
                "movement_type": event["movement__movement_type"],
                "direction": "in" if incoming else "out",
                "quantity_in": str(incoming),
                "quantity_out": str(outgoing),
                "unit_cost": str(event["unit_cost"]),
                "value_in": str(incoming_value),
                "value_out": str(outgoing_value),
                "balance_quantity": str(running_quantity),
                "balance_value": str(running_value),
            })

        closing_quantity, closing_value = totals(base)
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
            "closing_quantity": str(closing_quantity),
            "closing_value": str(closing_value),
            "rows": rows,
            "pagination": {
                "page": page, "page_size": page_size, "total_rows": total_rows,
                "total_pages": total_pages, "has_next": page < total_pages,
                "has_previous": page > 1,
            },
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



class InventoryCountViewSet(viewsets.ModelViewSet):
    serializer_class = InventoryCountSerializer
    permission_classes = (CompanyRolePermission,)
    queryset = InventoryCount.objects.select_related(
        "company", "warehouse", "warehouse__branch", "created_by", "posted_by",
        "gain_movement", "loss_movement",
    ).prefetch_related("lines__product__unit", "lines__location")
    filterset_fields = ("company", "warehouse", "status", "count_date")
    search_fields = ("count_number", "warehouse__name_ar", "notes")
    ordering_fields = ("count_date", "count_number", "created_at")

    def get_queryset(self):
        companies = accessible_companies(self.request.user)
        queryset = self.queryset.filter(company__in=companies)
        if self.request.user.is_superuser and not is_platform_owner(self.request.user):
            return queryset
        branches = accessible_branches(self.request.user)
        default_company_ids = _default_branch_company_ids(self.request.user, companies)
        return queryset.filter(
            Q(warehouse__branch__in=branches)
            | Q(warehouse__branch__isnull=True, company_id__in=default_company_ids)
        )

    def _check_count_write(self, count):
        if not can_access_company(self.request.user, count.company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الشركة المحددة.")
        if not has_company_role(self.request.user, count.company_id, ("admin", "accountant")):
            raise PermissionDenied("إجراءات الجرد متاحة لمدير الشركة والمحاسب فقط.")
        if not _can_access_warehouse(self.request.user, count.warehouse):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع المخزن.")

    @transaction.atomic
    def perform_create(self, serializer):
        company = serializer.validated_data["company"]
        warehouse = serializer.validated_data["warehouse"]
        if warehouse.company_id != company.pk or not warehouse.is_active:
            raise ValidationError("المخزن غير نشط أو لا يتبع الشركة.")
        if not can_access_company(self.request.user, company.pk):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الشركة المحددة.")
        if not has_company_role(self.request.user, company.pk, ("admin", "accountant")):
            raise PermissionDenied("بدء الجرد متاح لمدير الشركة والمحاسب فقط.")
        if not _can_access_warehouse(self.request.user, warehouse):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع المخزن.")
        warehouse = Warehouse.objects.select_for_update().get(pk=warehouse.pk)
        if InventoryCount.objects.filter(
            warehouse=warehouse, status=InventoryCount.Status.DRAFT,
        ).exists():
            raise ValidationError("يوجد مستند جرد مفتوح بالفعل على هذا المخزن.")
        pending = StockMovement.objects.filter(
            status=StockMovement.Status.DRAFT,
        ).filter(
            Q(source_warehouse_id=warehouse.pk) | Q(destination_warehouse_id=warehouse.pk)
        )
        if pending.exists():
            raise ValidationError("يوجد مستند حركة مسودة على المخزن. رحّله أو ألغِه قبل بدء الجرد.")
        count = serializer.save(created_by=self.request.user)
        balances = StockBalance.objects.filter(
            warehouse=warehouse, quantity__gt=0,
        ).select_related("product", "location").order_by(
            "product_id", "location_id", "expiry_date", "id",
        )
        InventoryCountLine.objects.bulk_create([
            InventoryCountLine(
                count=count,
                product=balance.product,
                location=balance.location,
                expiry_date=balance.expiry_date,
                system_quantity=balance.quantity,
                system_value=balance.inventory_value,
                adjustment_unit_cost=balance.average_cost,
            )
            for balance in balances
        ])

    @transaction.atomic
    def perform_update(self, serializer):
        count = InventoryCount.objects.select_for_update().get(pk=serializer.instance.pk)
        self._check_count_write(count)
        if count.status != InventoryCount.Status.DRAFT:
            raise PermissionDenied("لا يمكن تعديل جرد مرحّل أو ملغي.")
        serializer.save()

    @transaction.atomic
    def perform_destroy(self, instance):
        count = InventoryCount.objects.select_for_update().get(pk=instance.pk)
        self._check_count_write(count)
        if count.status != InventoryCount.Status.DRAFT:
            raise PermissionDenied("لا يمكن حذف جرد مرحّل أو ملغي.")
        if not has_company_role(self.request.user, count.company_id, ("admin",)):
            raise PermissionDenied("إلغاء مستند الجرد متاح لمدير الشركة فقط.")
        count.status = InventoryCount.Status.CANCELLED
        count.save(update_fields=("status", "updated_at"))

    @action(detail=True, methods=("post",), url_path="cancel")
    @transaction.atomic
    def cancel_count(self, request, pk=None):
        count = InventoryCount.objects.select_for_update().get(pk=self.get_object().pk)
        self._check_count_write(count)
        if count.status != InventoryCount.Status.DRAFT:
            raise ValidationError("لا يمكن إلغاء جرد مرحّل أو ملغي.")
        count.status = InventoryCount.Status.CANCELLED
        count.save(update_fields=("status", "updated_at"))
        return Response(self.get_serializer(count).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=("post",), url_path="post-count")
    def post_count(self, request, pk=None):
        from .counting import post_inventory_count

        count = self.get_object()
        self._check_count_write(count)
        count = post_inventory_count(count.pk, request.user)
        return Response(self.get_serializer(count).data, status=status.HTTP_200_OK)


class InventoryCountLineViewSet(viewsets.ModelViewSet):
    serializer_class = InventoryCountLineSerializer
    permission_classes = (CompanyRolePermission,)
    queryset = InventoryCountLine.objects.select_related(
        "count", "count__company", "count__warehouse", "count__warehouse__branch",
        "product", "product__unit", "location", "counted_by",
    )
    filterset_fields = ("count", "product", "count__company", "count__warehouse")
    search_fields = ("product__code", "product__name_ar", "notes")
    ordering_fields = ("product__code", "created_at")

    def get_queryset(self):
        companies = accessible_companies(self.request.user)
        queryset = self.queryset.filter(count__company__in=companies)
        if self.request.user.is_superuser and not is_platform_owner(self.request.user):
            return queryset
        branches = accessible_branches(self.request.user)
        default_company_ids = _default_branch_company_ids(self.request.user, companies)
        return queryset.filter(
            Q(count__warehouse__branch__in=branches)
            | Q(count__warehouse__branch__isnull=True, count__company_id__in=default_company_ids)
        )

    def _check_line_write(self, count):
        if not can_access_company(self.request.user, count.company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الشركة المحددة.")
        if not has_company_role(self.request.user, count.company_id, ("admin", "accountant")):
            raise PermissionDenied("إدخال الجرد متاح لمدير الشركة والمحاسب فقط.")
        if not _can_access_warehouse(self.request.user, count.warehouse):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع المخزن.")
        if count.status != InventoryCount.Status.DRAFT:
            raise PermissionDenied("لا يمكن تعديل بنود جرد مرحّل أو ملغي.")

    @transaction.atomic
    def perform_create(self, serializer):
        count = InventoryCount.objects.select_for_update().get(pk=serializer.validated_data["count"].pk)
        self._check_line_write(count)
        serializer.save(count=count, counted_by=self.request.user, counted_at=timezone.now())

    @transaction.atomic
    def perform_update(self, serializer):
        count = InventoryCount.objects.select_for_update().get(pk=serializer.instance.count_id)
        self._check_line_write(count)
        serializer.save(counted_by=self.request.user, counted_at=timezone.now())

    @transaction.atomic
    def perform_destroy(self, instance):
        count = InventoryCount.objects.select_for_update().get(pk=instance.count_id)
        self._check_line_write(count)
        if not has_company_role(self.request.user, count.company_id, ("admin",)):
            raise PermissionDenied("حذف بند الجرد متاح لمدير الشركة فقط.")
        instance.delete()

    @action(detail=True, methods=("post",), url_path="remove")
    @transaction.atomic
    def remove_extra_line(self, request, pk=None):
        line = self.get_object()
        count = InventoryCount.objects.select_for_update().get(pk=line.count_id)
        self._check_line_write(count)
        if line.system_quantity > 0:
            raise ValidationError("لا يمكن حذف بند من لقطة رصيد الدفاتر؛ أدخل الكمية الفعلية صفرًا إذا لم يوجد الصنف.")
        line.delete()
        return Response({"detail": "تم حذف بند الجرد الإضافي."}, status=status.HTTP_200_OK)


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

    @action(detail=False, methods=("get",), url_path="stock-aging")
    def stock_aging(self, request):
        """تحليل أعمار الحركة حسب آخر ترحيل لكل صنف ومخزن دون تحميل كل الحركات."""
        company_id = request.query_params.get("company")
        if not company_id:
            return Response({"detail": "حدد الشركة لتحليل حركة المخزون."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            company_id = int(company_id)
        except (TypeError, ValueError):
            return Response({"detail": "معرّف الشركة غير صحيح."}, status=status.HTTP_400_BAD_REQUEST)
        if not can_access_company(request.user, company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الشركة المحددة.")

        balances = self.get_queryset().filter(
            warehouse__company_id=company_id,
            product__product_type=Product.ProductType.GOODS,
            quantity__gt=0,
        )
        branch_id = request.query_params.get("branch")
        if branch_id:
            try:
                branch_id = int(branch_id)
            except (TypeError, ValueError):
                return Response({"detail": "معرّف الفرع غير صحيح."}, status=status.HTTP_400_BAD_REQUEST)
            if not can_access_branch(request.user, branch_id, company_id):
                raise PermissionDenied("لا تملك صلاحية الوصول إلى الفرع المحدد.")
            balances = balances.filter(warehouse__branch_id=branch_id)

        latest_movement_date = StockMovementLine.objects.filter(
            product_id=OuterRef("product_id"),
            movement__status=StockMovement.Status.POSTED,
        ).filter(
            Q(movement__source_warehouse_id=OuterRef("warehouse_id"))
            | Q(movement__destination_warehouse_id=OuterRef("warehouse_id"))
        ).order_by(
            "-movement__posted_at", "-movement_id", "-id",
        ).values("movement__movement_date")[:1]

        grouped = balances.values(
            "product_id", "product__code", "product__name_ar",
            "warehouse_id", "warehouse__name_ar",
        ).annotate(
            quantity=Sum("quantity"),
            value=Sum("inventory_value"),
            last_movement=Subquery(latest_movement_date),
        ).order_by(F("last_movement").asc(nulls_last=True), "product__code", "warehouse__name_ar")

        today = timezone.localdate()
        summary = {"active": 0, "slow": 0, "very_slow": 0, "stagnant": 0, "unknown": 0}
        rows = []
        total_rows = 0
        row_limit = 5000
        for item in grouped.iterator(chunk_size=1000):
            total_rows += 1
            last_movement = item["last_movement"]
            age_days = max(0, (today - last_movement).days) if last_movement else None
            age_band = (
                "unknown" if age_days is None else
                "active" if age_days <= 30 else
                "slow" if age_days <= 90 else
                "very_slow" if age_days <= 180 else "stagnant"
            )
            summary[age_band] += 1
            if len(rows) < row_limit:
                rows.append({
                    "product": item["product_id"],
                    "product_code": item["product__code"],
                    "product_name": item["product__name_ar"],
                    "warehouse": item["warehouse_id"],
                    "warehouse_name": item["warehouse__name_ar"],
                    "quantity": str(item["quantity"] or Decimal("0")),
                    "value": str((item["value"] or Decimal("0")).quantize(Decimal("0.0001"))),
                    "last_movement": last_movement.isoformat() if last_movement else None,
                    "age_days": age_days,
                    "age_band": age_band,
                })
        return Response({
            "company": company_id,
            "branch": branch_id,
            "as_of": today.isoformat(),
            "summary": summary,
            "total_rows": total_rows,
            "rows": rows,
            "truncated": total_rows > row_limit,
            "scope_note": "العمر محسوب من تاريخ آخر حركة مرحلة، مع ترتيب الأحداث حسب وقت الترحيل. التقرير مجمّع لكل صنف ومخزن ولا يمثل عمر طبقات التكلفة.",
        })

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

        balance_rows = StockBalance.objects.filter(
            warehouse__company_id=company_id,
            product__costing_method=Product.CostingMethod.FIFO,
        ).filter(warehouse_scope).values(
            "product_id", "warehouse_id", "location_id", "expiry_date", "quantity", "inventory_value",
            "product__code", "product__name_ar", "warehouse__code", "warehouse__name_ar",
        ).order_by(
            "product_id", "warehouse_id",
            F("location_id").asc(nulls_first=True), F("expiry_date").asc(nulls_first=True),
        ).iterator(chunk_size=2000)
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
        ).order_by(
            "product_id", "warehouse_id",
            F("location_id").asc(nulls_first=True), F("expiry_date").asc(nulls_first=True),
        ).iterator(chunk_size=2000)

        def dimension_key(row):
            return (row["product_id"], row["warehouse_id"], row["location_id"], row["expiry_date"])

        def dimension_sort_key(key):
            # Normalize nullable dimensions so the two ordered database streams can
            # be merged in constant memory without comparing None to integers/dates.
            return (key[0], key[1], key[2] or 0, key[3] or date.min)

        mismatches = []
        mismatch_count = 0

        def collect_mismatch(key, balance, layer):
            nonlocal mismatch_count
            balance_quantity = balance.get("quantity", Decimal("0")) or Decimal("0")
            balance_value = balance.get("inventory_value", Decimal("0")) or Decimal("0")
            layer_quantity = layer.get("quantity", Decimal("0")) or Decimal("0")
            layer_value = (layer.get("value", Decimal("0")) or Decimal("0")).quantize(Decimal("0.0001"))
            quantity_difference = balance_quantity - layer_quantity
            value_difference = balance_value - layer_value
            if abs(quantity_difference) > Decimal("0.0001") or abs(value_difference) > Decimal("0.0001"):
                mismatch_count += 1
                if len(mismatches) < 200:
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

        # Merge two identically ordered server-side streams. Memory use is bounded
        # by database iterator chunks plus the 200 mismatch details returned.
        balance_iter, layer_iter = iter(balance_rows), iter(layers)
        balance_row, layer_row = next(balance_iter, None), next(layer_iter, None)
        while balance_row is not None or layer_row is not None:
            if balance_row is None:
                collect_mismatch(dimension_key(layer_row), {}, layer_row)
                layer_row = next(layer_iter, None)
                continue
            if layer_row is None:
                collect_mismatch(dimension_key(balance_row), balance_row, {})
                balance_row = next(balance_iter, None)
                continue
            balance_key, layer_key = dimension_key(balance_row), dimension_key(layer_row)
            if balance_key == layer_key:
                collect_mismatch(balance_key, balance_row, layer_row)
                balance_row, layer_row = next(balance_iter, None), next(layer_iter, None)
            elif dimension_sort_key(balance_key) < dimension_sort_key(layer_key):
                collect_mismatch(balance_key, balance_row, {})
                balance_row = next(balance_iter, None)
            else:
                collect_mismatch(layer_key, {}, layer_row)
                layer_row = next(layer_iter, None)

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
            "fifo_mismatch_count": mismatch_count,
            "fifo_mismatches": mismatches,
            "pending_movements_count": pending_count,
            "pending_movements": pending_rows,
            "pending_movements_truncated": pending_count > len(pending_rows),
            "scope_branch_ids": branch_ids,
            "scope_note": "يشمل الفروع المسموح بها فقط؛ المخازن غير المرتبطة بفرع تُدرج مع المركز الرئيسي.",
        })

    @action(detail=False, methods=("get",), url_path="cost-aging")
    def cost_aging(self, request):
        """تقرير أعمار طبقات FIFO مع إعادة بناء الكمية المتبقية في تاريخ التقرير."""
        company_id = request.query_params.get("company")
        if not company_id:
            return Response({"detail": "حدد الشركة لعرض تقرير أعمار طبقات التكلفة."}, status=status.HTTP_400_BAD_REQUEST)
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

        as_of_raw = request.query_params.get("as_of", "")
        as_of = parse_date(as_of_raw) if as_of_raw else timezone.localdate()
        if as_of is None:
            return Response({"detail": "تاريخ التقرير غير صحيح؛ استخدم YYYY-MM-DD."}, status=status.HTTP_400_BAD_REQUEST)

        default_branch = Branch.objects.filter(
            company_id=company_id, is_active=True, is_head_office=True
        ).first()
        if default_branch is None:
            default_branch = Branch.objects.filter(company_id=company_id, is_active=True).order_by("code").first()
        include_unassigned = bool(default_branch and default_branch.pk in branch_ids)
        warehouse_scope = Q(warehouse__branch_id__in=branch_ids)
        if include_unassigned:
            warehouse_scope |= Q(warehouse__branch__isnull=True)

        # A layer can be consumed after the report date. Reconstruct its historical
        # quantity by adding back those later consumptions; only layers that existed
        # on the report date are eligible. This prevents a past "as of" report from
        # showing today's remaining quantity as if it were historical.
        decimal_quantity = DecimalField(max_digits=20, decimal_places=4)
        later_consumption_total = StockCostConsumption.objects.filter(
            source_layer_id=OuterRef("pk"),
            movement_line__movement__status=StockMovement.Status.POSTED,
            movement_line__movement__movement_date__gt=as_of,
        ).order_by().values("source_layer_id").annotate(
            total=Sum("quantity"),
        ).values("total")[:1]

        layers = StockCostLayer.objects.filter(
            product__company_id=company_id,
            product__costing_method=Product.CostingMethod.FIFO,
            source_line__movement__status=StockMovement.Status.POSTED,
            source_line__movement__movement_date__lte=as_of,
        ).filter(warehouse_scope).filter(
            Q(
                root_source_line__isnull=False,
                root_source_line__movement__status=StockMovement.Status.POSTED,
                root_source_line__movement__movement_date__lte=as_of,
            )
            | Q(root_source_line__isnull=True, parent_layer__isnull=True)
        ).annotate(
            receipt_date=Case(
                When(
                    root_source_line__isnull=False,
                    then=F("root_source_line__movement__movement_date"),
                ),
                default=F("source_line__movement__movement_date"),
                output_field=DateField(),
            ),
            consumed_after_as_of=Coalesce(
                Subquery(later_consumption_total, output_field=decimal_quantity),
                Value(Decimal("0.0000")),
                output_field=decimal_quantity,
            ),
        ).annotate(
            quantity_as_of=ExpressionWrapper(
                F("quantity_remaining") + F("consumed_after_as_of"),
                output_field=decimal_quantity,
            )
        ).filter(quantity_as_of__gt=0).select_related(
            "product", "warehouse", "location", "source_line__movement",
            "root_source_line__movement",
        )

        # Old transferred layers without a root reference cannot be aged safely.
        # Count them separately instead of silently presenting them as traceable.
        untraceable_legacy_layers = StockCostLayer.objects.filter(
            product__company_id=company_id,
            product__costing_method=Product.CostingMethod.FIFO,
            root_source_line__isnull=True,
            parent_layer__isnull=False,
            source_line__movement__status=StockMovement.Status.POSTED,
            source_line__movement__movement_date__lte=as_of,
        ).filter(warehouse_scope).annotate(
            consumed_after_as_of=Coalesce(
                Subquery(later_consumption_total, output_field=decimal_quantity),
                Value(Decimal("0.0000")),
                output_field=decimal_quantity,
            )
        ).annotate(
            quantity_as_of=ExpressionWrapper(
                F("quantity_remaining") + F("consumed_after_as_of"),
                output_field=decimal_quantity,
            )
        ).filter(quantity_as_of__gt=0).count()

        cut_30 = as_of - timedelta(days=30)
        cut_90 = as_of - timedelta(days=90)
        cut_180 = as_of - timedelta(days=180)

        def bucket_summary(queryset):
            totals = queryset.aggregate(
                layers=Count("id", distinct=True),
                quantity=Sum("quantity_as_of"),
                value=Sum(F("quantity_as_of") * F("unit_cost")),
            )
            return {
                "layers": totals["layers"] or 0,
                "quantity": str(totals["quantity"] or Decimal("0")),
                "value": str((totals["value"] or Decimal("0")).quantize(Decimal("0.0001"))),
            }

        buckets = {
            "active": bucket_summary(layers.filter(receipt_date__gte=cut_30)),
            "slow": bucket_summary(layers.filter(receipt_date__lt=cut_30, receipt_date__gte=cut_90)),
            "very_slow": bucket_summary(layers.filter(receipt_date__lt=cut_90, receipt_date__gte=cut_180)),
            "stagnant": bucket_summary(layers.filter(receipt_date__lt=cut_180)),
        }
        totals = bucket_summary(layers)
        rows = []
        row_limit = 500
        for layer in layers.order_by("receipt_date", "id")[:row_limit + 1]:
            receipt_line = layer.root_source_line or layer.source_line
            receipt_date = layer.receipt_date
            age_days = (as_of - receipt_date).days
            expiry_status = "none"
            if layer.expiry_date:
                days_to_expiry = (layer.expiry_date - as_of).days
                expiry_status = "expired" if days_to_expiry < 0 else "expiring" if days_to_expiry <= 30 else "valid"
            quantity_as_of = layer.quantity_as_of
            rows.append({
                "id": layer.pk,
                "product": layer.product_id,
                "product_code": layer.product.code,
                "product_name": layer.product.name_ar,
                "warehouse": layer.warehouse_id,
                "warehouse_name": layer.warehouse.name_ar,
                "location_name": layer.location.name_ar if layer.location_id else "",
                "source_movement_number": receipt_line.movement.movement_number,
                "receipt_date": receipt_date.isoformat(),
                "age_days": age_days,
                "quantity_remaining": str(quantity_as_of),
                "quantity_as_of": str(quantity_as_of),
                "unit_cost": str(layer.unit_cost),
                "layer_value": str((quantity_as_of * layer.unit_cost).quantize(Decimal("0.0001"))),
                "expiry_date": layer.expiry_date.isoformat() if layer.expiry_date else None,
                "expiry_status": expiry_status,
            })
        return Response({
            "company": company_id,
            "branch": selected_branch.pk if selected_branch else None,
            "branch_name": selected_branch.name_ar if selected_branch else "",
            "as_of": as_of.isoformat(),
            "basis": "تاريخ الاستلام الأصلي. الكمية التاريخية يعاد بناؤها بإضافة استهلاكات الطبقة التي حدثت بعد تاريخ التقرير.",
            "costing_method": "FIFO",
            "totals": totals,
            "buckets": buckets,
            "rows": rows[:row_limit],
            "truncated": totals["layers"] > row_limit,
            "untraceable_legacy_layers_count": untraceable_legacy_layers,
            "scope_branch_ids": branch_ids,
            "scope_note": "يعرض طبقات FIFO التي يمكن تتبع مصدرها فقط. الطبقات القديمة المنقولة دون رابط جذر تظهر في عداد منفصل. لا يعرض المتوسط المرجح كطبقات عمرية.",
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
            entry__status__in=(JournalEntry.Status.POSTED, JournalEntry.Status.REVERSED),
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
    
    @action(detail=False, methods=("get",), url_path="reorder-suggestions")
    def reorder_suggestions(self, request):
        """Suggest replenishment using on-hand, open sales commitments and open purchase orders."""
        company_id = request.query_params.get("company")
        if not company_id:
            raise ValidationError({"company": "حدد الشركة لاقتراح كميات الشراء."})
        try:
            company_id = int(company_id)
        except (TypeError, ValueError):
            raise ValidationError({"company": "معرّف الشركة غير صحيح."}) from None
        if not can_access_company(request.user, company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الشركة المحددة.")

        settings = WarehouseProductSetting.objects.filter(
            warehouse__company_id=company_id, warehouse__is_active=True,
            product__product_type=Product.ProductType.GOODS, product__is_active=True,
            is_active=True,
        ).select_related("product", "warehouse", "warehouse__branch")
        if not (request.user.is_superuser and not is_platform_owner(request.user)):
            branches = accessible_branches(request.user)
            defaults = _default_branch_company_ids(
                request.user, accessible_companies(request.user).filter(pk=company_id),
            )
            settings = settings.filter(
                Q(warehouse__branch__in=branches)
                | Q(warehouse__branch__isnull=True, warehouse__company_id__in=defaults)
            )
        branch_id = request.query_params.get("branch")
        if branch_id:
            try:
                branch_id = int(branch_id)
            except (TypeError, ValueError):
                raise ValidationError({"branch": "معرّف الفرع غير صحيح."}) from None
            if not can_access_branch(request.user, branch_id, company_id):
                raise PermissionDenied("لا تملك صلاحية الوصول إلى الفرع المحدد.")
            default_branch = _default_branch(company_id)
            settings = settings.filter(
                Q(warehouse__branch_id=branch_id)
                | (Q(warehouse__branch__isnull=True) if default_branch and default_branch.pk == branch_id else Q(pk__in=[]))
            )

        from sales.models import SalesOrder, SalesOrderLine
        from purchasing.models import PurchaseOrder, PurchaseOrderLine

        warehouse_ids = list(settings.values_list("warehouse_id", flat=True).distinct())
        product_ids = list(settings.values_list("product_id", flat=True).distinct())
        on_hand_rows = StockBalance.objects.filter(
            warehouse_id__in=warehouse_ids, product_id__in=product_ids,
        ).values("warehouse_id", "product_id").annotate(quantity=Sum("quantity"))
        on_hand = {
            (row["warehouse_id"], row["product_id"]): row["quantity"] or Decimal("0")
            for row in on_hand_rows
        }

        sales_lines = SalesOrderLine.objects.filter(
            order__warehouse_id__in=warehouse_ids,
            order__status__in=(SalesOrder.Status.CONFIRMED, SalesOrder.Status.PARTIALLY_DELIVERED),
            product_id__in=product_ids,
        ).annotate(
            delivered_quantity=Coalesce(
                Sum(
                    "delivery_allocations__quantity",
                    filter=Q(delivery_allocations__delivery__stock_movement__status=StockMovement.Status.POSTED),
                ),
                Value(Decimal("0")),
                output_field=DecimalField(max_digits=18, decimal_places=4),
            )
        ).values("order__warehouse_id", "product_id", "quantity", "delivered_quantity")
        reserved = {}
        for row in sales_lines.iterator(chunk_size=1000):
            remaining = max(row["quantity"] - row["delivered_quantity"], Decimal("0"))
            key = (row["order__warehouse_id"], row["product_id"])
            reserved[key] = reserved.get(key, Decimal("0")) + remaining

        purchase_lines = PurchaseOrderLine.objects.filter(
            order__warehouse_id__in=warehouse_ids,
            order__status__in=(PurchaseOrder.Status.CONFIRMED, PurchaseOrder.Status.PARTIALLY_RECEIVED),
            product_id__in=product_ids,
        ).values("order__warehouse_id", "product_id").annotate(
            remaining=Sum(F("quantity") - F("received_quantity")),
        )
        on_order = {
            (row["order__warehouse_id"], row["product_id"]): max(row["remaining"] or Decimal("0"), Decimal("0"))
            for row in purchase_lines
        }

        latest_movement = StockMovementLine.objects.filter(
            product_id=OuterRef("product_id"),
            movement__status=StockMovement.Status.POSTED,
        ).filter(
            Q(movement__source_warehouse_id=OuterRef("warehouse_id"))
            | Q(movement__destination_warehouse_id=OuterRef("warehouse_id"))
        ).order_by("-movement__posted_at", "-movement_id", "-id").values("movement__movement_date")[:1]
        settings = settings.annotate(last_movement=Subquery(latest_movement)).order_by(
            "warehouse__code", "product__code",
        )
        today = timezone.localdate()
        rows = []
        total = settings.count()
        for setting in settings[:5000]:
            key = (setting.warehouse_id, setting.product_id)
            quantity = on_hand.get(key, Decimal("0"))
            reserved_quantity = reserved.get(key, Decimal("0"))
            expected_quantity = on_order.get(key, Decimal("0"))
            available = quantity - reserved_quantity
            projected = available + expected_quantity
            minimum = setting.min_stock
            suggested = max(minimum - projected, Decimal("0"))
            age_days = max((today - setting.last_movement).days, 0) if setting.last_movement else None
            stock_status = (
                "sold" if quantity <= 0 else
                "reorder" if projected < minimum else
                "stagnant" if age_days is not None and age_days > 180 else "available"
            )
            rows.append({
                "product": setting.product_id, "product_code": setting.product.code,
                "product_name": setting.product.name_ar, "warehouse": setting.warehouse_id,
                "warehouse_name": setting.warehouse.name_ar,
                "on_hand": str(quantity), "reserved": str(reserved_quantity),
                "available": str(available), "on_order": str(expected_quantity),
                "projected_available": str(projected), "reorder_point": str(minimum),
                "suggested_purchase_quantity": str(suggested),
                "last_movement": setting.last_movement.isoformat() if setting.last_movement else None,
                "age_days": age_days, "stock_status": stock_status,
            })
        return Response({
            "company": company_id, "as_of": today.isoformat(),
            "count": total, "truncated": total > 5000, "rows": rows,
            "basis": "on_hand_minus_open_sales_orders_plus_open_purchase_orders",
        })
