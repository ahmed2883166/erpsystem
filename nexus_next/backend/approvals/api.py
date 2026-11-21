from django.db import transaction
from django.db.models import Q
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from accounting.permissions import CompanyRolePermission
from companies.access import accessible_branches, accessible_companies, can_access_company, has_company_role
from .models import DiscountApprovalPolicy, SalesInvoiceApproval
from .serializers import DiscountApprovalPolicySerializer, SalesInvoiceApprovalSerializer


class DiscountApprovalPolicyViewSet(viewsets.ModelViewSet):
    serializer_class = DiscountApprovalPolicySerializer
    permission_classes = (CompanyRolePermission,)
    http_method_names = ("get", "post", "patch", "head", "options")

    def get_queryset(self):
        return DiscountApprovalPolicy.objects.filter(company__in=accessible_companies(self.request.user)).select_related("company")

    def _admin(self, company_id):
        if not can_access_company(self.request.user, company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الشركة.")
        if not has_company_role(self.request.user, company_id, ("admin",)):
            raise PermissionDenied("إعداد حد الخصم متاح لمدير الشركة فقط.")

    def perform_create(self, serializer):
        company = serializer.validated_data["company"]
        self._admin(company.pk)
        serializer.save(created_by=self.request.user, updated_by=self.request.user)

    def perform_update(self, serializer):
        self._admin(serializer.instance.company_id)
        serializer.save(updated_by=self.request.user)

    def perform_destroy(self, instance):
        raise PermissionDenied("لا يمكن حذف سياسة الخصم؛ عدّل الحد أو أوقف استخدامه.")


class SalesInvoiceApprovalViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = SalesInvoiceApprovalSerializer
    permission_classes = (CompanyRolePermission,)

    def get_queryset(self):
        qs = SalesInvoiceApproval.objects.filter(
            company__in=accessible_companies(self.request.user)
        ).select_related("company", "invoice", "invoice__customer", "requested_by", "decided_by", "invoice__delivery__warehouse")
        if self.request.user.is_superuser:
            return qs
        return qs.filter(
            Q(invoice__delivery__warehouse__branch__isnull=True)
            | Q(invoice__delivery__warehouse__branch__in=accessible_branches(self.request.user))
        )

    def _manager(self, approval):
        if not has_company_role(self.request.user, approval.company_id, ("admin",)):
            raise PermissionDenied("اعتماد الخصومات متاح لمدير الشركة فقط.")
        if approval.requested_by_id == self.request.user.pk:
            raise PermissionDenied("لا يجوز اعتماد طلب الخصم بواسطة منشئه.")
        if approval.status != SalesInvoiceApproval.Status.PENDING:
            raise ValidationError("تم اتخاذ قرار على هذا الطلب بالفعل.")
        if approval.invoice.status != "draft":
            raise ValidationError("لا يمكن اعتماد طلب فاتورة ليست مسودة.")

    @action(detail=True, methods=("post",))
    @transaction.atomic
    def approve(self, request, pk=None):
        approval = self.get_object()
        approval = SalesInvoiceApproval.objects.select_for_update().select_related("invoice").get(pk=approval.pk)
        self._manager(approval)
        approval.status = SalesInvoiceApproval.Status.APPROVED
        approval.decided_by = request.user
        approval.decision_note = str(request.data.get("note", "")).strip()[:500]
        approval.save()
        return Response(self.get_serializer(approval).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=("post",))
    @transaction.atomic
    def reject(self, request, pk=None):
        approval = self.get_object()
        approval = SalesInvoiceApproval.objects.select_for_update().select_related("invoice").get(pk=approval.pk)
        self._manager(approval)
        note = str(request.data.get("note", "")).strip()
        if not note:
            raise ValidationError({"note": "سبب رفض الخصم مطلوب."})
        approval.status = SalesInvoiceApproval.Status.REJECTED
        approval.decided_by = request.user
        approval.decision_note = note[:500]
        approval.save()
        return Response(self.get_serializer(approval).data, status=status.HTTP_200_OK)
