from django.contrib.auth import get_user_model
from django.db.models import Q
from rest_framework import permissions, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from .access import accessible_branches, accessible_companies, can_access_company, has_company_role
from .models import Branch, CompanyMembership, Currency, FiscalYear
from .serializers import BranchSerializer, CompanyMembershipSerializer, CompanySerializer, CurrencySerializer, FiscalYearSerializer


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
        from rest_framework.exceptions import PermissionDenied, ValidationError
        if not has_company_role(self.request.user, instance.pk, ("admin",)):
            raise PermissionDenied("حذف الشركة متاح لمدير الشركة فقط.")
        if instance.audit_events.exists():
            raise ValidationError("لا يمكن حذف شركة لها سجل تدقيق؛ أوقف الشركة بدل حذفها.")
        instance.delete()



class CompanyMembershipViewSet(viewsets.ModelViewSet):
    serializer_class = CompanyMembershipSerializer
    permission_classes = (CompanyAdminOrReadOnly,)
    http_method_names = ("get", "post", "patch", "head", "options")

    def get_queryset(self):
        qs = CompanyMembership.objects.filter(
            company__in=accessible_companies(self.request.user)
        ).select_related("company", "user").prefetch_related("branches")
        if not self.request.user.is_superuser:
            qs = qs.filter(
                company__memberships__user=self.request.user,
                company__memberships__is_active=True,
                company__memberships__role=CompanyMembership.Role.ADMIN,
            ).distinct()
        company_id = self.request.query_params.get("company")
        return qs.filter(company_id=company_id) if company_id else qs

    def _require_admin(self, company_id):
        from rest_framework.exceptions import PermissionDenied
        if not has_company_role(self.request.user, company_id, ("admin",)):
            raise PermissionDenied("إدارة المستخدمين والصلاحيات متاحة لمدير الشركة فقط.")

    def get_object(self):
        instance = super().get_object()
        self._require_admin(instance.company_id)
        return instance

    def create(self, request, *args, **kwargs):
        from rest_framework.exceptions import ValidationError
        try:
            company_id = int(request.data.get("company", ""))
        except (TypeError, ValueError):
            raise ValidationError({"company": "حدد الشركة أولًا."})
        self._require_admin(company_id)
        return super().create(request, *args, **kwargs)

    def _protect_last_admin(self, membership, new_role, new_active):
        from rest_framework.exceptions import ValidationError
        remains_admin = (
            membership.is_active and membership.role == CompanyMembership.Role.ADMIN
            and (not new_active or new_role != CompanyMembership.Role.ADMIN)
        )
        if remains_admin:
            active_admins = CompanyMembership.objects.filter(
                company_id=membership.company_id, is_active=True,
                role=CompanyMembership.Role.ADMIN,
            ).count()
            if active_admins <= 1:
                raise ValidationError("لا يمكن تعطيل أو تخفيض صلاحية آخر مدير نشط للشركة.")

    @action(detail=False, methods=("get",), url_path="current")
    def current(self, request):
        from rest_framework.exceptions import NotFound, ValidationError
        try:
            company_id = int(request.query_params.get("company", ""))
        except (TypeError, ValueError):
            raise ValidationError({"company": "حدد الشركة أولًا."})
        membership = CompanyMembership.objects.filter(
            user=request.user, company_id=company_id, is_active=True,
        ).first()
        if not membership and not request.user.is_superuser:
            raise NotFound("لا توجد عضوية نشطة للمستخدم في الشركة.")
        if request.user.is_superuser:
            return Response({"role": "admin", "all_branches": True, "branches": []})
        return Response(self.get_serializer(membership).data)

    @action(detail=False, methods=("get",), url_path="users")
    def users(self, request):
        from rest_framework.exceptions import ValidationError
        try:
            company_id = int(request.query_params.get("company", ""))
        except (TypeError, ValueError):
            raise ValidationError({"company": "حدد الشركة أولًا."})
        self._require_admin(company_id)
        term = request.query_params.get("search", "").strip()
        if len(term) < 2:
            return Response([])
        User = get_user_model()
        users = User.objects.filter(is_active=True).filter(
            Q(username__icontains=term) | Q(display_name__icontains=term)
        ).exclude(company_memberships__company_id=company_id).order_by("username")[:25]
        return Response([
            {"id": user.pk, "username": user.username, "display_name": user.display_name}
            for user in users
        ])

    def perform_create(self, serializer):
        company = serializer.validated_data["company"]
        self._require_admin(company.pk)
        serializer.save()

    def perform_update(self, serializer):
        instance = serializer.instance
        self._require_admin(instance.company_id)
        new_role = serializer.validated_data.get("role", instance.role)
        new_active = serializer.validated_data.get("is_active", instance.is_active)
        self._protect_last_admin(instance, new_role, new_active)
        serializer.save()

    def perform_destroy(self, instance):
        from rest_framework.exceptions import PermissionDenied
        raise PermissionDenied("لا تحذف عضويات المستخدمين؛ عطّل العضوية للاحتفاظ بسجل الصلاحيات.")


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
        from rest_framework.exceptions import PermissionDenied, ValidationError
        if not has_company_role(self.request.user, instance.company_id, ("admin",)):
            raise PermissionDenied("حذف الفروع متاح لمدير الشركة فقط.")
        if instance.audit_events.exists():
            raise ValidationError("لا يمكن حذف فرع له سجل تدقيق؛ عطّل الفرع بدل حذفه.")
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
