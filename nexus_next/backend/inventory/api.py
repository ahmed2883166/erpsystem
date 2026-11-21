from django.db.models import Q
from rest_framework import viewsets
from rest_framework.exceptions import PermissionDenied

from accounting.permissions import CompanyRolePermission
from companies.access import accessible_branches, accessible_companies, can_access_branch, can_access_company, has_company_role

from .models import Product, ProductCategory, StorageLocation, UnitOfMeasure, Warehouse
from .serializers import (
    ProductCategorySerializer, ProductSerializer, StorageLocationSerializer,
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

    def perform_destroy(self, instance):
        self._check_write(instance.warehouse)
        instance.is_active = False
        instance.save(update_fields=("is_active",))
