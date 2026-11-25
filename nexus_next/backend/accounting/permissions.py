from rest_framework.permissions import BasePermission, SAFE_METHODS

from companies.access import has_company_role


class CompanyRolePermission(BasePermission):
    """Company membership roles control accounting writes; queryset scoping controls reads."""

    message = "ليس لديك الدور أو الصلاحية المطلوبة لتنفيذ هذا الإجراء."

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated)

    def has_object_permission(self, request, view, obj):
        if request.method in SAFE_METHODS:
            return True
        roles = ("admin",) if view.action == "destroy" else ("admin", "accountant")
        return has_company_role(request.user, obj.company_id, roles)
