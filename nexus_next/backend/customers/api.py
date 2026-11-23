from rest_framework import viewsets
from rest_framework.exceptions import PermissionDenied, ValidationError

from accounting.permissions import CompanyRolePermission
from companies.access import accessible_companies, can_access_company, has_company_role, is_platform_owner

from .models import Customer, CustomerDiscountRule
from .serializers import CustomerSerializer, CustomerDiscountRuleSerializer


class CustomerViewSet(viewsets.ModelViewSet):
    serializer_class = CustomerSerializer
    permission_classes = (CompanyRolePermission,)
    queryset = Customer.objects.select_related("company").all()
    filterset_fields = ("company", "customer_type", "is_active")
    search_fields = ("code", "name_ar", "name_en", "tax_number", "phone", "mobile", "email")
    ordering_fields = ("code", "name_ar", "created_at", "credit_limit")

    def get_queryset(self):
        """تجهيز السجلات التي يمكن للمستخدم الوصول إليها ضمن شركته وصلاحياته."""
        if self.request.user.is_superuser and not is_platform_owner(self.request.user):
            return self.queryset
        return self.queryset.filter(
            company__in=accessible_companies(self.request.user)
        )

    def _check_company_write(self, company_id):
        """التحقق من صلاحية الكتابة داخل الشركة قبل تنفيذ التعديل."""
        if not can_access_company(self.request.user, company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الشركة المحددة.")
        if not has_company_role(self.request.user, company_id, ("admin", "accountant")):
            raise PermissionDenied("إضافة وتعديل العملاء متاحة لمدير الشركة والمحاسب فقط.")

    def perform_create(self, serializer):
        """حفظ العميل أو المورد بعد التحقق من نطاق الشركة."""
        self._check_company_write(serializer.validated_data["company"].pk)
        serializer.save()

    def perform_update(self, serializer):
        """حفظ التعديل بعد التحقق من صلاحية الشركة وثبات نطاق السجل."""
        company = serializer.validated_data.get("company", serializer.instance.company)
        if company.pk != serializer.instance.company_id:
            raise ValidationError("لا يمكن نقل سجل عميل إلى شركة أخرى بعد إنشائه.")
        self._check_company_write(company.pk)
        serializer.save()

    def perform_destroy(self, instance):
        """منع الحذف النهائي والحفاظ على مراجع المستندات المحاسبية التاريخية."""
        self._check_company_write(instance.company_id)
        raise ValidationError("لا يمكن حذف العميل نهائيًا؛ أوقفه من خلال حالة النشاط للحفاظ على السجلات التاريخية.")



class CustomerDiscountRuleViewSet(viewsets.ModelViewSet):
    """إدارة قواعد الخصم ضمن نطاق الشركة مع منع الوصول بين الشركات."""
    serializer_class = CustomerDiscountRuleSerializer
    permission_classes = (CompanyRolePermission,)
    queryset = CustomerDiscountRule.objects.select_related(
        "company", "customer", "product", "category"
    ).all()
    filterset_fields = ("company", "customer", "customer_type", "product", "category", "is_active")
    search_fields = ("customer__name_ar", "product__name_ar", "category__name_ar", "notes")
    ordering_fields = ("priority", "effective_from", "discount_percent", "created_at")

    def get_queryset(self):
        return self.queryset.filter(company__in=accessible_companies(self.request.user))

    def _check_write(self, company_id):
        if not can_access_company(self.request.user, company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الشركة المحددة.")
        if not has_company_role(self.request.user, company_id, ("admin", "accountant")):
            raise PermissionDenied("إدارة قواعد الخصم متاحة لمدير الشركة والمحاسب فقط.")

    def perform_create(self, serializer):
        self._check_write(serializer.validated_data["company"].pk)
        serializer.save()

    def perform_update(self, serializer):
        self._check_write(serializer.instance.company_id)
        company = serializer.validated_data.get("company", serializer.instance.company)
        if company.pk != serializer.instance.company_id:
            raise ValidationError("لا يمكن نقل قاعدة الخصم إلى شركة أخرى.")
        serializer.save()

    def perform_destroy(self, instance):
        self._check_write(instance.company_id)
        instance.is_active = False
        instance.save(update_fields=("is_active", "updated_at"))
