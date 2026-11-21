from rest_framework import permissions, viewsets

from .access import accessible_branches, accessible_companies, can_access_company, has_company_role
from .models import Branch, CompanyMembership, Currency, FiscalYear
from .serializers import BranchSerializer, CompanySerializer, CurrencySerializer, FiscalYearSerializer


class ReadOnlyOrAdmin(permissions.BasePermission):
    def has_permission(self, request, view):
        if request.method in permissions.SAFE_METHODS:
            return bool(request.user and request.user.is_authenticated)
        return bool(request.user and request.user.is_staff)


class CompanyAdminOrReadOnly(permissions.BasePermission):
    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated)


class CurrencyViewSet(viewsets.ModelViewSet):
    queryset = Currency.objects.all()
    serializer_class = CurrencySerializer
    permission_classes = (ReadOnlyOrAdmin,)


class CompanyViewSet(viewsets.ModelViewSet):
    serializer_class = CompanySerializer
    permission_classes = (CompanyAdminOrReadOnly,)

    def get_queryset(self):
        return accessible_companies(self.request.user).select_related("base_currency")

    def perform_create(self, serializer):
        if not self.request.user.is_staff:
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied("إنشاء شركة جديدة يتطلب صلاحية مسؤول النظام.")
        company = serializer.save()
        CompanyMembership.objects.get_or_create(
            user=self.request.user, company=company,
            defaults={"role": CompanyMembership.Role.ADMIN, "all_branches": True, "is_active": True},
        )

    def perform_update(self, serializer):
        if not has_company_role(self.request.user, self.get_object().pk, ("admin",)):
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied("إدارة بيانات الشركة متاحة لمدير الشركة فقط.")
        serializer.save()

    def perform_destroy(self, instance):
        if not has_company_role(self.request.user, instance.pk, ("admin",)):
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied("حذف الشركة متاح لمدير الشركة فقط.")
        instance.delete()


class BranchViewSet(viewsets.ModelViewSet):
    serializer_class = BranchSerializer
    permission_classes = (CompanyAdminOrReadOnly,)

    def get_queryset(self):
        return accessible_branches(self.request.user).select_related("company")

    def perform_create(self, serializer):
        company = serializer.validated_data["company"]
        if not has_company_role(self.request.user, company.pk, ("admin",)):
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied("إنشاء الفروع متاح لمدير الشركة فقط.")
        serializer.save()

    def perform_update(self, serializer):
        from rest_framework.exceptions import PermissionDenied
        current_company_id = serializer.instance.company_id
        target_company = serializer.validated_data.get("company", serializer.instance.company)
        if not has_company_role(self.request.user, current_company_id, ("admin",)):
            raise PermissionDenied("تعديل الفروع متاح لمدير الشركة فقط.")
        if not has_company_role(self.request.user, target_company.pk, ("admin",)):
            raise PermissionDenied("لا تملك صلاحية نقل الفرع إلى الشركة المحددة.")
        serializer.save()

    def perform_destroy(self, instance):
        from rest_framework.exceptions import PermissionDenied
        if not has_company_role(self.request.user, instance.company_id, ("admin",)):
            raise PermissionDenied("حذف الفروع متاح لمدير الشركة فقط.")
        instance.delete()


class FiscalYearViewSet(viewsets.ModelViewSet):
    serializer_class = FiscalYearSerializer
    permission_classes = (CompanyAdminOrReadOnly,)

    def get_queryset(self):
        return FiscalYear.objects.filter(
            company__in=accessible_companies(self.request.user)
        ).select_related("company")

    def perform_create(self, serializer):
        company = serializer.validated_data["company"]
        if not has_company_role(self.request.user, company.pk, ("admin",)):
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied("إنشاء السنوات المالية متاح لمدير الشركة فقط.")
        serializer.save()

    def perform_update(self, serializer):
        from rest_framework.exceptions import PermissionDenied
        current_company_id = serializer.instance.company_id
        target_company = serializer.validated_data.get("company", serializer.instance.company)
        if not has_company_role(self.request.user, current_company_id, ("admin",)):
            raise PermissionDenied("تعديل السنوات المالية متاح لمدير الشركة فقط.")
        if not has_company_role(self.request.user, target_company.pk, ("admin",)):
            raise PermissionDenied("لا تملك صلاحية نقل السنة المالية إلى الشركة المحددة.")
        serializer.save()

    def perform_destroy(self, instance):
        from rest_framework.exceptions import PermissionDenied
        if not has_company_role(self.request.user, instance.company_id, ("admin",)):
            raise PermissionDenied("حذف السنوات المالية متاح لمدير الشركة فقط.")
        instance.delete()
