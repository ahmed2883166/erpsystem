from rest_framework import permissions, viewsets
from .models import Branch, Company, Currency, FiscalYear
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
    queryset = Company.objects.select_related("base_currency").all()
    serializer_class = CompanySerializer
    permission_classes = (ReadOnlyOrAdmin,)

class BranchViewSet(viewsets.ModelViewSet):
    queryset = Branch.objects.select_related("company").all()
    serializer_class = BranchSerializer
    permission_classes = (ReadOnlyOrAdmin,)

class FiscalYearViewSet(viewsets.ModelViewSet):
    queryset = FiscalYear.objects.select_related("company").all()
    serializer_class = FiscalYearSerializer
    permission_classes = (ReadOnlyOrAdmin,)
