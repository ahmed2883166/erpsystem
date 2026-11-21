from rest_framework import viewsets
from rest_framework.exceptions import PermissionDenied

from accounting.permissions import CompanyRolePermission
from companies.access import accessible_companies, can_access_company, has_company_role

from .models import Customer
from .serializers import CustomerSerializer


class CustomerViewSet(viewsets.ModelViewSet):
    serializer_class = CustomerSerializer
    permission_classes = (CompanyRolePermission,)
    queryset = Customer.objects.select_related("company").all()
    filterset_fields = ("company", "customer_type", "is_active")
    search_fields = ("code", "name_ar", "name_en", "tax_number", "phone", "mobile", "email")
    ordering_fields = ("code", "name_ar", "created_at", "credit_limit")

    def get_queryset(self):
        if self.request.user.is_superuser:
            return self.queryset
        return self.queryset.filter(
            company__in=accessible_companies(self.request.user)
        )

    def _check_company_write(self, company_id):
        if not can_access_company(self.request.user, company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الشركة المحددة.")
        if not has_company_role(self.request.user, company_id, ("admin", "accountant")):
            raise PermissionDenied("إضافة وتعديل العملاء متاحة لمدير الشركة والمحاسب فقط.")

    def perform_create(self, serializer):
        self._check_company_write(serializer.validated_data["company"].pk)
        serializer.save()

    def perform_update(self, serializer):
        company = serializer.validated_data.get("company", serializer.instance.company)
        self._check_company_write(company.pk)
        serializer.save()
