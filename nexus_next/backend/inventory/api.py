from django.db.models import Q
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from accounting.permissions import CompanyRolePermission
from companies.access import accessible_branches, accessible_companies, can_access_branch, can_access_company, has_company_role

from .models import Product, ProductCategory, StockMovement, StorageLocation, UnitOfMeasure, Warehouse
from .serializers import (
    ProductCategorySerializer, ProductSerializer, StockMovementSerializer, StorageLocationSerializer,
    UnitOfMeasureSerializer, WarehouseSerializer,
)


class CompanyScopedViewSet(viewsets.ModelViewSet):
    permission_classes = (CompanyRolePermission,)
    company_field = "company"

    def _check_company_write(self, company_id):
        if not can_access_company(self.request.user, company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الشركة المحددة.")
        if not has_company_role(self.request.user, company_id, ("admin", "accountant")):
            raise PermissionDenied("التعديل متاح لمدير الشركة والمحاسب فقط.")

    def create(self, request, *args, **kwargs):
        # Reject unauthorized writes before serializer validation can expose field errors.
        company_id = request.data.get("company")
        if company_id not in (None, ""):
            try:
                self._check_company_write(int(company_id))
            except (TypeError, ValueError):
                pass
        return super().create(request, *args, **kwargs)

    def perform_create(self, serializer):
        company = serializer.validated_data.get("company")
        if company is None:
            raise PermissionDenied("يجب تحديد الشركة.")
        self._check_company_write(company.pk)
        self._check_related_scope(serializer.validated_data)
        serializer.save()

    def perform_update(self, serializer):
        company = serializer.validated_data.get("company", serializer.instance.company)
        self._check_company_write(company.pk)
        self._check_related_scope(serializer.validated_data)
        serializer.save()

    def _check_related_scope(self, data):
        return None

    def perform_destroy(self, instance):
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
        return self.queryset.filter(company__in=accessible_companies(self.request.user))


class UnitOfMeasureViewSet(CompanyScopedViewSet):
    serializer_class = UnitOfMeasureSerializer
    queryset = UnitOfMeasure.objects.select_related("company")
    filterset_fields = ("company", "is_active")
    search_fields = ("code", "name_ar", "symbol")
    ordering_fields = ("code", "name_ar")

    def get_queryset(self):
        return self.queryset.filter(company__in=accessible_companies(self.request.user))


class ProductViewSet(CompanyScopedViewSet):
    serializer_class = ProductSerializer
    queryset = Product.objects.select_related("company", "category", "unit")
    filterset_fields = ("company", "category", "product_type", "is_active", "track_expiry")
    search_fields = ("code", "barcode", "name_ar", "name_en")
    ordering_fields = ("code", "name_ar", "sale_price", "purchase_price", "created_at")

    def get_queryset(self):
        return self.queryset.filter(company__in=accessible_companies(self.request.user))


class WarehouseViewSet(CompanyScopedViewSet):
    serializer_class = WarehouseSerializer
    queryset = Warehouse.objects.select_related("company", "branch")
    filterset_fields = ("company", "branch", "is_active")
    search_fields = ("code", "name_ar")
    ordering_fields = ("code", "name_ar", "created_at")

    def get_queryset(self):
        companies = accessible_companies(self.request.user)
        if self.request.user.is_superuser:
            return self.queryset.filter(company__in=companies)
        branches = accessible_branches(self.request.user)
        return self.queryset.filter(company__in=companies).filter(
            Q(branch__isnull=True) | Q(branch__in=branches)
        )

    def _check_related_scope(self, data):
        branch = data.get("branch")
        company = data.get("company")
        if branch and not can_access_branch(
            self.request.user, branch.pk, company.pk if company else None
        ):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الفرع المحدد.")


class StorageLocationViewSet(viewsets.ModelViewSet):
    serializer_class = StorageLocationSerializer
    permission_classes = (CompanyRolePermission,)
    queryset = StorageLocation.objects.select_related("warehouse", "warehouse__company", "parent")
    filterset_fields = ("warehouse", "location_type", "is_active")
    search_fields = ("code", "name_ar", "warehouse__name_ar")
    ordering_fields = ("code", "name_ar")

    def get_queryset(self):
        companies = accessible_companies(self.request.user)
        if self.request.user.is_superuser:
            return self.queryset.filter(warehouse__company__in=companies)
        branches = accessible_branches(self.request.user)
        return self.queryset.filter(warehouse__company__in=companies).filter(
            Q(warehouse__branch__isnull=True) | Q(warehouse__branch__in=branches)
        )

    def create(self, request, *args, **kwargs):
        warehouse_id = request.data.get("warehouse")
        if warehouse_id not in (None, ""):
            warehouse = Warehouse.objects.filter(pk=warehouse_id).first()
            if warehouse:
                self._check_write(warehouse)
        return super().create(request, *args, **kwargs)

    def perform_destroy(self, instance):
        self._check_write(instance.warehouse)
        instance.is_active = False
        instance.save(update_fields=("is_active",))

    def _check_write(self, warehouse):
        if not can_access_company(self.request.user, warehouse.company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الشركة المحددة.")
        if not has_company_role(self.request.user, warehouse.company_id, ("admin", "accountant")):
            raise PermissionDenied("التعديل متاح لمدير الشركة والمحاسب فقط.")
        if warehouse.branch_id and not can_access_branch(self.request.user, warehouse.branch_id, warehouse.company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع المخزن.")

    def perform_create(self, serializer):
        warehouse = serializer.validated_data["warehouse"]
        self._check_write(warehouse)
        serializer.save()

    def perform_update(self, serializer):
        warehouse = serializer.validated_data.get("warehouse", serializer.instance.warehouse)
        self._check_write(warehouse)
        serializer.save()


class StockMovementViewSet(viewsets.ModelViewSet):
    serializer_class = StockMovementSerializer
    permission_classes = (CompanyRolePermission,)
    queryset = StockMovement.objects.select_related(
        "company", "source_warehouse", "destination_warehouse", "created_by", "posted_by"
    ).prefetch_related("lines__product")
    filterset_fields = ("company", "movement_type", "status", "movement_date")
    search_fields = ("movement_number", "notes", "lines__product__code", "lines__product__name_ar")
    ordering_fields = ("movement_date", "movement_number", "created_at")

    def get_queryset(self):
        companies = accessible_companies(self.request.user)
        queryset = self.queryset.filter(company__in=companies)
        if self.request.user.is_superuser:
            return queryset
        branches = accessible_branches(self.request.user)
        return queryset.filter(
            Q(source_warehouse__isnull=True) | Q(source_warehouse__branch__isnull=True) | Q(source_warehouse__branch__in=branches),
            Q(destination_warehouse__isnull=True) | Q(destination_warehouse__branch__isnull=True) | Q(destination_warehouse__branch__in=branches),
        ).distinct()

    def create(self, request, *args, **kwargs):
        company_id = request.data.get("company")
        if company_id not in (None, ""):
            try:
                CompanyScopedViewSet._check_company_write(self, int(company_id))
            except (TypeError, ValueError):
                pass
        return super().create(request, *args, **kwargs)

    def _check_warehouse(self, warehouse, company_id):
        if warehouse.company_id != company_id:
            raise PermissionDenied("المخزن لا يتبع الشركة المحددة.")
        if not can_access_company(self.request.user, company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الشركة.")
        if not has_company_role(self.request.user, company_id, ("admin", "accountant")):
            raise PermissionDenied("التعديل متاح لمدير الشركة والمحاسب فقط.")
        if warehouse.branch_id and not can_access_branch(self.request.user, warehouse.branch_id, company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع المخزن.")

    def perform_create(self, serializer):
        company = serializer.validated_data["company"]
        CompanyScopedViewSet._check_company_write(self, company.pk)
        for key in ("source_warehouse", "destination_warehouse"):
            warehouse = serializer.validated_data.get(key)
            if warehouse:
                self._check_warehouse(warehouse, company.pk)
        serializer.save(created_by=self.request.user)

    def perform_update(self, serializer):
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
        if instance.status != StockMovement.Status.DRAFT:
            raise PermissionDenied("لا يمكن حذف حركة مرحلة.")
        if not has_company_role(self.request.user, instance.company_id, ("admin",)):
            raise PermissionDenied("حذف المسودات متاح لمدير الشركة فقط.")
        instance.delete()

    @action(detail=True, methods=("post",))
    def post(self, request, pk=None):
        from .services import post_stock_movement
        movement = self.get_object()
        self._check_warehouse(movement.source_warehouse, movement.company_id) if movement.source_warehouse else None
        self._check_warehouse(movement.destination_warehouse, movement.company_id) if movement.destination_warehouse else None
        movement = post_stock_movement(movement.pk, request.user)
        return Response(self.get_serializer(movement).data, status=status.HTTP_200_OK)
