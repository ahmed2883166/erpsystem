from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView

from .performance import performance_snapshot


class PerformanceMetricsView(APIView):
    permission_classes = (IsAdminUser,)

    def get(self, request):
        return Response(performance_snapshot())
