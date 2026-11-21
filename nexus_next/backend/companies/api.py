from rest_framework import permissions, viewsets

from .access import accessible_branches, accessible_companies, can_access_company
from .models import Branch, CompanyMembership, Currency, FiscalYear
from .serializers import BranchSerializer, CompanySerializer, CurrencySerializer, FiscalYearSerializer


class ReadOnlyOrAdmin(permissions.BasePermission):
    def has_permission(self, request, view):
        if request.method in permissions.SAFE_METHODS:
            return bool(request.user and request.user.is_authenticated)
        return bool(request.user and request.user.is_staff)


class CurrencyViewSet(viewsets.ModelViewSet):
    queryset = Currency.objects.all()
    serializer_class = CurrencySerializer
    permission_classes = (ReadOnlyOrAdmin,)


class CompanyViewSet(viewsets.ModelViewSet):
    serializer_class = CompanySerializer
    permission_classes = (ReadOnlyOrAdmin,)

    def get_queryset(self):
        return accessible_companies(self.request.user).select_related("base_currency")

    def perform_create(self, serializer):
        company = serializer.save()
        CompanyMembership.objects.get_or_create(
            user=self.request.user, company=company,
            defaults={"role": CompanyMembership.Role.ADMIN, "all_branches": True, "is_active": True},
        )

    def perform_update(self, serializer):
        if not can_access_company(self.request.user, self.get_object().pk):
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied("لا تملك صلاحية الوصول إلى هذه الشركة.")
        serializer.save()


class BranchViewSet(viewsets.ModelViewSet):
    serializer_class = BranchSerializer
    permission_classes = (ReadOnlyOrAdmin,)

    def get_queryset(self):
        return accessible_branches(self.request.user).select_related("company")

    def perform_create(self, serializer):
        company = serializer.validated_data["company"]
        if not can_access_company(self.request.user, company.pk):
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied("لا تملك صلاحية إنشاء فرع في هذه الشركة.")
        serializer.save()

    def perform_update(self, serializer):
        company = serializer.validated_data.get("company", serializer.instance.company)
        if not can_access_company(self.request.user, company.pk):
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied("لا تملك صلاحية نقل الفرع إلى هذه الشركة.")
        serializer.save()


class FiscalYearViewSet(viewsets.ModelViewSet):
    serializer_class = FiscalYearSerializer
    permission_classes = (ReadOnlyOrAdmin,)

    def get_queryset(self):
        return FiscalYear.objects.filter(
            company__in=accessible_companies(self.request.user)
        ).select_related("company")

    def perform_create(self, serializer):
        company = serializer.validated_data["company"]
        if not can_access_company(self.request.user, company.pk):
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied("لا تملك صلاحية إنشاء سنة مالية لهذه الشركة.")
        serializer.save()

    def perform_update(self, serializer):
        company = serializer.validated_data.get("company", serializer.instance.company)
        if not can_access_company(self.request.user, company.pk):
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied("لا تملك صلاحية نقل السنة المالية إلى هذه الشركة.")
        serializer.save()
