from datetime import date
from decimal import Decimal

from django.db.models import Q, Sum
from django.utils.dateparse import parse_date
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.response import Response

from accounting.models import AccountRole, JournalEntry, JournalLine
from accounting.permissions import CompanyRolePermission
from companies.access import accessible_branches, accessible_companies, can_access_branch, can_access_company, has_company_role
from companies.models import Branch

from .models import Product, ProductCategory, StockBalance, StockMovement, StorageLocation, UnitOfMeasure, Warehouse, WarehouseProductSetting
from .serializers import (
    ProductCategorySerializer, ProductSerializer, StockBalanceSerializer, StockMovementSerializer, StorageLocationSerializer,
    UnitOfMeasureSerializer, WarehouseProductSettingSerializer, WarehouseSerializer,
)


class CompanyScopedViewSet(viewsets.ModelViewSet):
    permission_classes = (CompanyRolePermission,)
    company_field = "company"

    def _check_company_write(self, company_id):
        if not can_access_company(self.request.user, company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الشركة المحددة.")
        if not has_company_role(self.request.user, company_id, ("admin", "accountant")):
            raise PermissionDenied("التعديل متاح لمدير الشركة والمحاسب فقط.")

    def create(self, request, *args, **kwargs):
        # Reject unauthorized writes before serializer validation can expose field errors.
        company_id = request.data.get("company")
        if company_id not in (None, ""):
            try:
                self._check_company_write(int(company_id))
            except (TypeError, ValueError):
                pass
        return super().create(request, *args, **kwargs)

    def perform_create(self, serializer):
        company = serializer.validated_data.get("company")
        if company is None:
            raise PermissionDenied("يجب تحديد الشركة.")
        self._check_company_write(company.pk)
        self._check_related_scope(serializer.validated_data)
        serializer.save()

    def perform_update(self, serializer):
        company = serializer.validated_data.get("company", serializer.instance.company)
        self._check_company_write(company.pk)
        self._check_related_scope(serializer.validated_data)
        serializer.save()

    def _check_related_scope(self, data):
        return None

    def perform_destroy(self, instance):
        if not has_company_role(self.request.user, instance.company_id, ("admin",)):
            raise PermissionDenied("حذف البيانات متاح لمدير الشركة فقط.")
        instance.is_active = False
        instance.save(update_fields=("is_active",))


class ProductCategoryViewSet(CompanyScopedViewSet):
    serializer_class = ProductCategorySerializer
    queryset = ProductCategory.objects.select_related("company", "parent")
    filterset_fields = ("company", "parent", "is_active")
    search_fields = ("code", "name_ar")
    ordering_fields = ("code", "name_ar")

    def get_queryset(self):
        return self.queryset.filter(company__in=accessible_companies(self.request.user))


class UnitOfMeasureViewSet(CompanyScopedViewSet):
    serializer_class = UnitOfMeasureSerializer
    queryset = UnitOfMeasure.objects.select_related("company")
    filterset_fields = ("company", "is_active")
    search_fields = ("code", "name_ar", "symbol")
    ordering_fields = ("code", "name_ar")

    def get_queryset(self):
        return self.queryset.filter(company__in=accessible_companies(self.request.user))


class ProductViewSet(CompanyScopedViewSet):
    serializer_class = ProductSerializer
    queryset = Product.objects.select_related("company", "category", "unit")
    filterset_fields = ("company", "category", "product_type", "is_active", "track_expiry")
    search_fields = ("code", "barcode", "name_ar", "name_en")
    ordering_fields = ("code", "name_ar", "sale_price", "purchase_price", "created_at")

    def get_queryset(self):
        return self.queryset.filter(company__in=accessible_companies(self.request.user))


class WarehouseViewSet(CompanyScopedViewSet):
    serializer_class = WarehouseSerializer
    queryset = Warehouse.objects.select_related("company", "branch")
    filterset_fields = ("company", "branch", "is_active")
    search_fields = ("code", "name_ar")
    ordering_fields = ("code", "name_ar", "created_at")

    def get_queryset(self):
        companies = accessible_companies(self.request.user)
        if self.request.user.is_superuser:
            return self.queryset.filter(company__in=companies)
        branches = accessible_branches(self.request.user)
        return self.queryset.filter(company__in=companies).filter(
            Q(branch__isnull=True) | Q(branch__in=branches)
        )

    def _check_related_scope(self, data):
        branch = data.get("branch")
        company = data.get("company")
        if branch and not can_access_branch(
            self.request.user, branch.pk, company.pk if company else None
        ):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الفرع المحدد.")


class WarehouseProductSettingViewSet(viewsets.ModelViewSet):
    serializer_class = WarehouseProductSettingSerializer
    permission_classes = (CompanyRolePermission,)
    queryset = WarehouseProductSetting.objects.select_related("product", "warehouse", "warehouse__company")
    filterset_fields = ("warehouse", "product", "warehouse__company", "is_active")
    search_fields = ("product__code", "product__name_ar", "warehouse__code", "warehouse__name_ar")
    ordering_fields = ("warehouse__code", "product__code", "min_stock", "updated_at")

    def get_queryset(self):
        companies = accessible_companies(self.request.user)
        queryset = self.queryset.filter(warehouse__company__in=companies)
        if self.request.user.is_superuser:
            return queryset
        branches = accessible_branches(self.request.user)
        return queryset.filter(Q(warehouse__branch__isnull=True) | Q(warehouse__branch__in=branches))

    def _check_write(self, company_id):
        if not can_access_company(self.request.user, company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الشركة المحددة.")
        if not has_company_role(self.request.user, company_id, ("admin", "accountant")):
            raise PermissionDenied("التعديل متاح لمدير الشركة والمحاسب فقط.")

    def perform_create(self, serializer):
        warehouse = serializer.validated_data["warehouse"]
        self._check_write(warehouse.company_id)
        serializer.save()

    def perform_update(self, serializer):
        warehouse = serializer.validated_data.get("warehouse", serializer.instance.warehouse)
        self._check_write(warehouse.company_id)
        serializer.save()

    def perform_destroy(self, instance):
        if not has_company_role(self.request.user, instance.warehouse.company_id, ("admin",)):
            raise PermissionDenied("حذف الإعدادات متاح لمدير الشركة فقط.")
        instance.is_active = False
        instance.save(update_fields=("is_active",))


class StorageLocationViewSet(viewsets.ModelViewSet):
    serializer_class = StorageLocationSerializer
    permission_classes = (CompanyRolePermission,)
    queryset = StorageLocation.objects.select_related("warehouse", "warehouse__company", "parent")
    filterset_fields = ("warehouse", "warehouse__company", "location_type", "is_active")
    search_fields = ("code", "name_ar", "warehouse__name_ar")
    ordering_fields = ("code", "name_ar")

    def get_queryset(self):
        companies = accessible_companies(self.request.user)
        if self.request.user.is_superuser:
            return self.queryset.filter(warehouse__company__in=companies)
        branches = accessible_branches(self.request.user)
        return self.queryset.filter(warehouse__company__in=companies).filter(
            Q(warehouse__branch__isnull=True) | Q(warehouse__branch__in=branches)
        )

    def create(self, request, *args, **kwargs):
        warehouse_id = request.data.get("warehouse")
        if warehouse_id not in (None, ""):
            warehouse = Warehouse.objects.filter(pk=warehouse_id).first()
            if warehouse:
                self._check_write(warehouse)
        return super().create(request, *args, **kwargs)

    def perform_destroy(self, instance):
        self._check_write(instance.warehouse)
        instance.is_active = False
        instance.save(update_fields=("is_active",))

    def _check_write(self, warehouse):
        if not can_access_company(self.request.user, warehouse.company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الشركة المحددة.")
        if not has_company_role(self.request.user, warehouse.company_id, ("admin", "accountant")):
            raise PermissionDenied("التعديل متاح لمدير الشركة والمحاسب فقط.")
        if warehouse.branch_id and not can_access_branch(self.request.user, warehouse.branch_id, warehouse.company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع المخزن.")

    def perform_create(self, serializer):
        warehouse = serializer.validated_data["warehouse"]
        self._check_write(warehouse)
        serializer.save()

    def perform_update(self, serializer):
        warehouse = serializer.validated_data.get("warehouse", serializer.instance.warehouse)
        self._check_write(warehouse)
        serializer.save()


class StockMovementViewSet(viewsets.ModelViewSet):
    serializer_class = StockMovementSerializer
    permission_classes = (CompanyRolePermission,)
    queryset = StockMovement.objects.select_related(
        "company", "source_warehouse", "destination_warehouse", "created_by", "posted_by"
    ).prefetch_related("lines__product", "journal_links__journal_entry__branch")
    filterset_fields = ("company", "movement_type", "status", "movement_date")
    search_fields = ("movement_number", "notes", "lines__product__code", "lines__product__name_ar")
    ordering_fields = ("movement_date", "movement_number", "created_at")

    def get_queryset(self):
        companies = accessible_companies(self.request.user)
        queryset = self.queryset.filter(company__in=companies)
        if self.request.user.is_superuser:
            return queryset
        branches = accessible_branches(self.request.user)
        return queryset.filter(
            Q(source_warehouse__isnull=True) | Q(source_warehouse__branch__isnull=True) | Q(source_warehouse__branch__in=branches),
            Q(destination_warehouse__isnull=True) | Q(destination_warehouse__branch__isnull=True) | Q(destination_warehouse__branch__in=branches),
        ).distinct()

    def create(self, request, *args, **kwargs):
        company_id = request.data.get("company")
        if company_id not in (None, ""):
            try:
                CompanyScopedViewSet._check_company_write(self, int(company_id))
            except (TypeError, ValueError):
                pass
        return super().create(request, *args, **kwargs)

    def _check_warehouse(self, warehouse, company_id):
        if not warehouse.is_active:
            raise PermissionDenied("لا يمكن استخدام مخزن موقوف.")
        if warehouse.company_id != company_id:
            raise PermissionDenied("المخزن لا يتبع الشركة المحددة.")
        if not can_access_company(self.request.user, company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الشركة.")
        if not has_company_role(self.request.user, company_id, ("admin", "accountant")):
            raise PermissionDenied("التعديل متاح لمدير الشركة والمحاسب فقط.")
        if warehouse.branch_id and not can_access_branch(self.request.user, warehouse.branch_id, company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع المخزن.")

    def perform_create(self, serializer):
        company = serializer.validated_data["company"]
        CompanyScopedViewSet._check_company_write(self, company.pk)
        for key in ("source_warehouse", "destination_warehouse"):
            warehouse = serializer.validated_data.get(key)
            if warehouse:
                self._check_warehouse(warehouse, company.pk)
        serializer.save(created_by=self.request.user)

    def perform_update(self, serializer):
        movement = serializer.instance
        if movement.status != StockMovement.Status.DRAFT:
            raise PermissionDenied("لا يمكن تعديل حركة مرحلة.")
        CompanyScopedViewSet._check_company_write(self, movement.company_id)
        for key in ("source_warehouse", "destination_warehouse"):
            warehouse = serializer.validated_data.get(key, getattr(movement, key))
            if warehouse:
                self._check_warehouse(warehouse, movement.company_id)
        serializer.save()

    def perform_destroy(self, instance):
        if instance.status != StockMovement.Status.DRAFT:
            raise PermissionDenied("لا يمكن حذف حركة مرحلة.")
        if not has_company_role(self.request.user, instance.company_id, ("admin",)):
            raise PermissionDenied("حذف المسودات متاح لمدير الشركة فقط.")
        instance.delete()

    @action(detail=False, methods=("get",), url_path="stock-card")
    def stock_card(self, request):
        product_id = request.query_params.get("product")
        warehouse_id = request.query_params.get("warehouse")
        if not product_id or not warehouse_id:
            return Response({"detail": "حدد الصنف والمخزن لعرض بطاقة الصنف."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            product_id, warehouse_id = int(product_id), int(warehouse_id)
        except (TypeError, ValueError):
            return Response({"detail": "معرّف الصنف أو المخزن غير صحيح."}, status=status.HTTP_400_BAD_REQUEST)

        companies = accessible_companies(request.user)
        product = Product.objects.filter(pk=product_id, company__in=companies, product_type=Product.ProductType.GOODS).first()
        if product is None:
            raise NotFound("الصنف غير موجود أو غير متاح.")
        warehouse_query = Warehouse.objects.filter(pk=warehouse_id, company_id=product.company_id, is_active=True)
        if not request.user.is_superuser:
            branches = accessible_branches(request.user)
            warehouse_query = warehouse_query.filter(Q(branch__isnull=True) | Q(branch__in=branches))
        warehouse = warehouse_query.first()
        if warehouse is None:
            raise NotFound("المخزن غير موجود أو غير متاح.")

        date_from_raw, date_to_raw = request.query_params.get("date_from"), request.query_params.get("date_to")
        date_from = parse_date(date_from_raw) if date_from_raw else None
        date_to = parse_date(date_to_raw) if date_to_raw else None
        if (date_from_raw and date_from is None) or (date_to_raw and date_to is None):
            return Response({"detail": "صيغة التاريخ يجب أن تكون YYYY-MM-DD."}, status=status.HTTP_400_BAD_REQUEST)
        if date_from and date_to and date_from > date_to:
            return Response({"detail": "تاريخ البداية يجب ألا يتجاوز تاريخ النهاية."}, status=status.HTTP_400_BAD_REQUEST)

        movements = self.get_queryset().filter(
            status=StockMovement.Status.POSTED,
            lines__product_id=product_id,
        ).filter(Q(source_warehouse_id=warehouse_id) | Q(destination_warehouse_id=warehouse_id)).distinct().prefetch_related("lines")
        events = []
        for movement in movements:
            direction = "in" if movement.destination_warehouse_id == warehouse_id else "out"
            for line in movement.lines.all():
                if line.product_id != product_id:
                    continue
                events.append({
                    "date": movement.movement_date,
                    "movement_number": movement.movement_number,
                    "movement_type": movement.movement_type,
                    "direction": direction,
                    "quantity": line.quantity,
                    "unit_cost": line.unit_cost,
                    "cost_amount": line.cost_amount,
                })
        events.sort(key=lambda item: (item["date"], item["movement_number"]))
        opening_quantity, opening_value = Decimal("0"), Decimal("0")
        period_events = []
        for event in events:
            if date_from and event["date"] < date_from:
                sign = Decimal("1") if event["direction"] == "in" else Decimal("-1")
                opening_quantity += sign * event["quantity"]
                opening_value += sign * event["cost_amount"]
            elif date_to and event["date"] > date_to:
                continue
            else:
                period_events.append(event)

        running_quantity, running_value = opening_quantity, opening_value
        rows = []
        for event in period_events:
            incoming = event["quantity"] if event["direction"] == "in" else Decimal("0")
            outgoing = event["quantity"] if event["direction"] == "out" else Decimal("0")
            incoming_value = event["cost_amount"] if event["direction"] == "in" else Decimal("0")
            outgoing_value = event["cost_amount"] if event["direction"] == "out" else Decimal("0")
            running_quantity += incoming - outgoing
            running_value += incoming_value - outgoing_value
            rows.append({
                "date": event["date"].isoformat(),
                "movement_number": event["movement_number"],
                "movement_type": event["movement_type"],
                "direction": event["direction"],
                "quantity_in": str(incoming),
                "quantity_out": str(outgoing),
                "unit_cost": str(event["unit_cost"]),
                "value_in": str(incoming_value),
                "value_out": str(outgoing_value),
                "balance_quantity": str(running_quantity),
                "balance_value": str(running_value),
            })
        return Response({
            "product": product.pk,
            "product_code": product.code,
            "product_name": product.name_ar,
            "warehouse": warehouse.pk,
            "warehouse_name": warehouse.name_ar,
            "date_from": date_from.isoformat() if date_from else None,
            "date_to": date_to.isoformat() if date_to else None,
            "opening_quantity": str(opening_quantity),
            "opening_value": str(opening_value),
            "closing_quantity": str(running_quantity),
            "closing_value": str(running_value),
            "rows": rows,
        })

    @action(detail=True, methods=("post",))
    def post(self, request, pk=None):
        from .services import post_stock_movement
        movement = self.get_object()
        self._check_warehouse(movement.source_warehouse, movement.company_id) if movement.source_warehouse else None
        self._check_warehouse(movement.destination_warehouse, movement.company_id) if movement.destination_warehouse else None
        movement = post_stock_movement(movement.pk, request.user)
        return Response(self.get_serializer(movement).data, status=status.HTTP_200_OK)



class StockBalanceViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = StockBalanceSerializer
    permission_classes = (CompanyRolePermission,)
    queryset = StockBalance.objects.select_related("product", "warehouse", "warehouse__company")
    filterset_fields = ("warehouse", "product", "warehouse__company", "location", "expiry_date")
    search_fields = ("product__code", "product__name_ar", "warehouse__name_ar")
    ordering_fields = ("product__code", "warehouse__code", "quantity", "inventory_value")

    def get_queryset(self):
        companies = accessible_companies(self.request.user)
        queryset = self.queryset.filter(warehouse__company__in=companies)
        if self.request.user.is_superuser:
            return queryset
        branches = accessible_branches(self.request.user)
        return queryset.filter(
            Q(warehouse__branch__isnull=True) | Q(warehouse__branch__in=branches)
        )

    @action(detail=False, methods=("get",), url_path="reconciliation")
    def reconciliation(self, request):
        company_id = request.query_params.get("company")
        if not company_id:
            return Response({"detail": "حدد الشركة لإجراء مطابقة المخزون."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            company_id = int(company_id)
        except (TypeError, ValueError):
            return Response({"detail": "معرّف الشركة غير صحيح."}, status=status.HTTP_400_BAD_REQUEST)
        if not can_access_company(request.user, company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الشركة المحددة.")

        role = AccountRole.objects.select_related("account").filter(
            company_id=company_id, role=AccountRole.Role.INVENTORY
        ).first()
        if role is None:
            return Response({"detail": "لم يتم ربط دور حساب المخزون بدليل الحسابات."}, status=status.HTTP_400_BAD_REQUEST)
        inventory_account = role.account
        if (
            not inventory_account.is_active or inventory_account.is_group
            or not inventory_account.allow_posting
            or inventory_account.nature != inventory_account.Nature.ASSET
        ):
            return Response({"detail": "حساب المخزون المرتبط يجب أن يكون حساب أصول تفصيليًا ونشطًا وقابلًا للترحيل."}, status=status.HTTP_400_BAD_REQUEST)

        # Never expose company-wide totals to a user who can access only selected branches.
        accessible_ids = list(
            accessible_branches(request.user, company_id).values_list("id", flat=True)
        )
        requested_branch = request.query_params.get("branch")
        if requested_branch:
            try:
                requested_branch_id = int(requested_branch)
            except (TypeError, ValueError):
                return Response({"detail": "معرّف الفرع غير صحيح."}, status=status.HTTP_400_BAD_REQUEST)
            selected_branch = Branch.objects.filter(
                pk=requested_branch_id, company_id=company_id, is_active=True
            ).first()
            if selected_branch is None:
                return Response({"detail": "الفرع المحدد غير موجود أو موقوف."}, status=status.HTTP_400_BAD_REQUEST)
            if selected_branch.pk not in accessible_ids:
                raise PermissionDenied("لا تملك صلاحية الوصول إلى الفرع المحدد.")
            branch_ids = [selected_branch.pk]
        else:
            selected_branch = None
            branch_ids = accessible_ids

        default_branch = Branch.objects.filter(
            company_id=company_id, is_active=True, is_head_office=True
        ).first()
        if default_branch is None:
            default_branch = Branch.objects.filter(
                company_id=company_id, is_active=True
            ).order_by("code").first()

        stock_scope = Q(warehouse__branch_id__in=branch_ids)
        # Warehouses without an explicit branch post their inventory journals to
        # the same default branch selected by inventory.posting._branch_for().
        if default_branch and default_branch.pk in branch_ids:
            stock_scope |= Q(warehouse__branch__isnull=True)
        stock_totals = StockBalance.objects.filter(
            warehouse__company_id=company_id
        ).filter(stock_scope).aggregate(total=Sum("inventory_value"))
        stock_value = stock_totals["total"] or Decimal("0")

        gl_totals = JournalLine.objects.filter(
            account_id=inventory_account.pk,
            entry__company_id=company_id,
            entry__branch_id__in=branch_ids,
            entry__status=JournalEntry.Status.POSTED,
        ).aggregate(debit=Sum("debit"), credit=Sum("credit"))
        gl_balance = (gl_totals["debit"] or Decimal("0")) - (gl_totals["credit"] or Decimal("0"))
        difference = gl_balance - stock_value
        return Response({
            "company": company_id,
            "branch": selected_branch.pk if selected_branch else None,
            "branch_code": selected_branch.code if selected_branch else "",
            "branch_name": selected_branch.name_ar if selected_branch else "",
            "scope_branch_ids": branch_ids,
            "scope_note": "عند اختيار كل الفروع، تُحسب الفروع المسموح للمستخدم بالوصول إليها فقط. المخازن غير المرتبطة بفرع تُنسب للمركز الرئيسي، مثل قيود ترحيل المخزون.",
            "account_id": inventory_account.pk,
            "account_code": inventory_account.code,
            "account_name": inventory_account.name_ar,
            "stock_value": str(stock_value),
            "gl_balance": str(gl_balance),
            "difference": str(difference),
            "is_reconciled": difference == 0,
            "basis": "current_stock_snapshot_vs_posted_inventory_account_balance",
        })
