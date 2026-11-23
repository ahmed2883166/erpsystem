from rest_framework.permissions import BasePermission
from rest_framework.response import Response
from rest_framework.views import APIView

from companies.access import is_platform_owner
from .performance import performance_snapshot


class IsOperationalStaff(BasePermission):
    """Allow staff diagnostics but keep platform-owner identities outside tenant operations."""

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_staff and not is_platform_owner(request.user))


class PerformanceMetricsView(APIView):
    permission_classes = (IsOperationalStaff,)

    def get(self, request):
        """عرض مؤشرات الأداء والتشغيل للمستخدم الإداري المصرح له."""
        return Response(performance_snapshot())
