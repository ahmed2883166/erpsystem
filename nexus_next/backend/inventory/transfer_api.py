from django.db.models import Q
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from accounting.permissions import CompanyRolePermission
from companies.access import accessible_branches, accessible_companies, can_access_branch, can_access_company, has_company_role, is_platform_owner

from .models import StockTransfer, StockTransferReceipt
from .transfer_serializers import (
    StockTransferReceiveSerializer,
    StockTransferReceiptSerializer,
    StockTransferSerializer,
)
from .transfer_services import dispatch_stock_transfer, receive_stock_transfer


def _can_access_transfer_warehouse(user, warehouse):
    if warehouse.branch_id:
        return can_access_branch(user, warehouse.branch_id, warehouse.company_id)
    from companies.models import Branch
    branch = Branch.objects.filter(
        company_id=warehouse.company_id, is_active=True,
    ).order_by("-is_head_office", "code").first()
    return bool(branch and can_access_branch(user, branch.pk, warehouse.company_id))


def _check_transfer_warehouse(user, warehouse, company_id):
    if warehouse.company_id != company_id or not warehouse.is_active or warehouse.is_transit:
        raise PermissionDenied("مخزن التحويل غير نشط أو لا يتبع الشركة أو ليس مخزنًا تشغيليًا.")
    if not can_access_company(user, company_id):
        raise PermissionDenied("لا تملك صلاحية الوصول إلى الشركة.")
    if not has_company_role(user, company_id, ("admin", "accountant")):
        raise PermissionDenied("التعديل متاح لمدير الشركة والمحاسب فقط.")
    if not _can_access_transfer_warehouse(user, warehouse):
        raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع أحد مخزني التحويل.")


class StockTransferViewSet(viewsets.ModelViewSet):
    serializer_class = StockTransferSerializer
    permission_classes = (CompanyRolePermission,)
    queryset = StockTransfer.objects.select_related(
        "company", "source_warehouse", "source_warehouse__branch",
        "destination_warehouse", "destination_warehouse__branch",
        "created_by", "dispatched_by", "dispatch_movement",
    ).prefetch_related("lines__product__unit", "receipts")

    filterset_fields = ("company", "status", "transfer_date")
    search_fields = ("transfer_number", "notes", "lines__product__code", "lines__product__name_ar")
    ordering_fields = ("transfer_date", "transfer_number", "created_at")

    def get_queryset(self):
        companies = accessible_companies(self.request.user)
        queryset = self.queryset.filter(company__in=companies)
        if self.request.user.is_superuser and not is_platform_owner(self.request.user):
            return queryset
        branches = accessible_branches(self.request.user)
        default_company_ids = []
        from companies.models import Branch
        for company_id in companies.values_list("id", flat=True):
            branch = Branch.objects.filter(
                company_id=company_id, is_active=True,
            ).order_by("-is_head_office", "code").first()
            if branch and can_access_branch(self.request.user, branch.pk, company_id):
                default_company_ids.append(company_id)
        source_scope = (
            Q(source_warehouse__branch__in=branches)
            | Q(source_warehouse__branch__isnull=True, company_id__in=default_company_ids)
        )
        destination_scope = (
            Q(destination_warehouse__branch__in=branches)
            | Q(destination_warehouse__branch__isnull=True, company_id__in=default_company_ids)
        )
        # Either participating branch may read the transfer. Dispatch and receive
        # actions enforce source- and destination-branch permissions separately.
        return queryset.filter(source_scope | destination_scope).distinct()

    def perform_create(self, serializer):
        company = serializer.validated_data["company"]
        if not can_access_company(self.request.user, company.pk):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الشركة.")
        if not has_company_role(self.request.user, company.pk, ("admin", "accountant")):
            raise PermissionDenied("إنشاء التحويل متاح لمدير الشركة والمحاسب فقط.")
        # The sending branch owns initiation. The receiving branch is allowed
        # to receive later without requiring membership in the sending branch.
        _check_transfer_warehouse(self.request.user, serializer.validated_data["source_warehouse"], company.pk)
        serializer.save(created_by=self.request.user)

    def perform_update(self, serializer):
        transfer = serializer.instance
        if transfer.status != StockTransfer.Status.DRAFT:
            raise PermissionDenied("لا يمكن تعديل تحويل تم شحنه أو استلامه.")
        # Only the source branch may edit a draft transfer; serializer validation
        # still guarantees that both warehouses are active and in this company.
        _check_transfer_warehouse(self.request.user, serializer.validated_data.get("source_warehouse", transfer.source_warehouse), transfer.company_id)
        serializer.save()

    def perform_destroy(self, instance):
        if instance.status != StockTransfer.Status.DRAFT:
            raise PermissionDenied("لا يمكن حذف تحويل تم شحنه.")
        if not has_company_role(self.request.user, instance.company_id, ("admin",)):
            raise PermissionDenied("حذف مسودات التحويل متاح لمدير الشركة فقط.")
        instance.delete()

    @action(detail=True, methods=("post",), url_path="dispatch")
    def dispatch_transfer(self, request, pk=None):
        transfer = self.get_object()
        _check_transfer_warehouse(request.user, transfer.source_warehouse, transfer.company_id)
        transfer = dispatch_stock_transfer(transfer.pk, request.user)
        return Response(self.get_serializer(transfer).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=("post",))
    def receive(self, request, pk=None):
        transfer = self.get_object()
        _check_transfer_warehouse(request.user, transfer.destination_warehouse, transfer.company_id)
        serializer = StockTransferReceiveSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        receipt = receive_stock_transfer(
            transfer.pk,
            request.user,
            receipt_date=serializer.validated_data["receipt_date"],
            lines=serializer.validated_data["lines"],
        )
        receipt = StockTransferReceipt.objects.select_related(
            "transfer", "movement", "received_by",
        ).prefetch_related("lines__transfer_line__product").get(pk=receipt.pk)
        return Response(StockTransferReceiptSerializer(receipt).data, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=("get",), url_path="in-transit")
    def in_transit(self, request):
        queryset = self.filter_queryset(self.get_queryset()).filter(
            status__in=(StockTransfer.Status.IN_TRANSIT, StockTransfer.Status.PARTIALLY_RECEIVED),
        ).prefetch_related("lines__product__unit")
        rows = []
        for transfer in queryset:
            lines = [{
                "transfer_line": line.pk,
                "product": line.product_id,
                "product_code": line.product.code,
                "product_name": line.product.name_ar,
                "unit": line.product.unit.symbol or line.product.unit.name_ar,
                "dispatched_quantity": str(line.quantity),
                "received_quantity": str(line.received_quantity),
                "remaining_quantity": str(line.remaining_quantity),
                "expiry_date": line.expiry_date.isoformat() if line.expiry_date else None,
            } for line in transfer.lines.all() if line.remaining_quantity > 0]
            if lines:
                rows.append({
                    "id": transfer.pk,
                    "transfer_number": transfer.transfer_number,
                    "transfer_date": transfer.transfer_date.isoformat(),
                    "source_warehouse": transfer.source_warehouse.name_ar,
                    "destination_warehouse": transfer.destination_warehouse.name_ar,
                    "status": transfer.status,
                    "lines": lines,
                })
        return Response(rows)


class StockTransferReceiptViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = StockTransferReceiptSerializer
    permission_classes = (CompanyRolePermission,)
    queryset = StockTransferReceipt.objects.select_related(
        "transfer", "transfer__company", "transfer__source_warehouse",
        "transfer__destination_warehouse", "movement", "received_by",
    ).prefetch_related("lines__transfer_line__product")

    filterset_fields = ("transfer", "receipt_date")

    def get_queryset(self):
        companies = accessible_companies(self.request.user)
        queryset = self.queryset.filter(transfer__company__in=companies)
        if self.request.user.is_superuser and not is_platform_owner(self.request.user):
            return queryset
        branches = accessible_branches(self.request.user)
        from companies.models import Branch
        default_company_ids = []
        for company_id in companies.values_list("id", flat=True):
            branch = Branch.objects.filter(
                company_id=company_id, is_active=True,
            ).order_by("-is_head_office", "code").first()
            if branch and can_access_branch(self.request.user, branch.pk, company_id):
                default_company_ids.append(company_id)
        source_scope = (
            Q(transfer__source_warehouse__branch__in=branches)
            | Q(transfer__source_warehouse__branch__isnull=True, transfer__company_id__in=default_company_ids)
        )
        destination_scope = (
            Q(transfer__destination_warehouse__branch__in=branches)
            | Q(transfer__destination_warehouse__branch__isnull=True, transfer__company_id__in=default_company_ids)
        )
        # Both participating branches need visibility into the receipt audit trail.
        return queryset.filter(source_scope | destination_scope).distinct()
