from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from rest_framework import decorators, permissions, response, status, viewsets
from rest_framework.exceptions import PermissionDenied, ValidationError

from companies.access import accessible_branches, accessible_companies, can_access_branch, can_access_company, has_company_role
from .models import FixedAsset, AssetDepreciation
from .serializers import FixedAssetSerializer, AssetDepreciationSerializer
from .services import acquire_asset, depreciate_asset


class FixedAssetViewSet(viewsets.ModelViewSet):
    permission_classes = (permissions.IsAuthenticated,)
    serializer_class = FixedAssetSerializer
    queryset = FixedAsset.objects.select_related("company", "branch", "asset_account",
        "accumulated_depreciation_account", "depreciation_expense_account",
        "acquisition_counter_account", "disposal_gain_loss_account", "acquisition_journal").prefetch_related("depreciations").all()

    def get_queryset(self):
        qs = self.queryset.filter(company__in=accessible_companies(self.request.user))
        company_id = self.request.query_params.get("company")
        if company_id:
            if not company_id.isdigit() or not can_access_company(self.request.user, int(company_id)):
                raise PermissionDenied("لا تملك صلاحية الوصول إلى الشركة المحددة.")
            qs = qs.filter(company_id=int(company_id))
        if not self.request.user.is_superuser:
            qs = qs.filter(branch__in=accessible_branches(self.request.user))
        return qs

    def _write(self, company_id):
        if not has_company_role(self.request.user, company_id, ("admin", "accountant")):
            raise PermissionDenied("تحتاج إلى دور مدير الشركة أو المحاسب.")

    def perform_create(self, serializer):
        data = serializer.validated_data
        if not can_access_company(self.request.user, data["company"].pk) or not can_access_branch(self.request.user, data["branch"].pk, data["company"].pk):
            raise PermissionDenied("لا تملك صلاحية الوصول للشركة أو الفرع.")
        self._write(data["company"].pk)
        try:
            with transaction.atomic():
                asset = serializer.save()
                acquire_asset(asset.pk, self.request.data.get("fiscal_year"), self.request.user)
        except (DjangoValidationError, TypeError, ValueError) as exc:
            detail = exc.message_dict if hasattr(exc, "message_dict") else getattr(exc, "messages", [str(exc)])
            raise ValidationError(detail) from exc

    def perform_update(self, serializer):
        if serializer.instance.acquisition_journal_id:
            raise ValidationError("الأصل المثبت محاسبيًا لا يمكن تعديل بيانات تكلفته أو حساباته.")
        self._write(serializer.instance.company_id)
        serializer.save()

    def perform_destroy(self, instance):
        self._write(instance.company_id)
        if instance.acquisition_journal_id or instance.depreciations.exists():
            raise ValidationError("لا يمكن حذف أصل له أثر محاسبي؛ استخدم إجراء الاستبعاد.")
        instance.delete()

    @decorators.action(detail=True, methods=("post",))
    def depreciate(self, request, pk=None):
        asset = self.get_object()
        self._write(asset.company_id)
        try:
            amount = depreciate_asset(asset.pk, request.data.get("period_date"), request.data.get("fiscal_year"), request.user)
        except (DjangoValidationError, TypeError, ValueError) as exc:
            detail = exc.message_dict if hasattr(exc, "message_dict") else getattr(exc, "messages", [str(exc)])
            raise ValidationError(detail) from exc
        return response.Response({"asset_id": asset.pk, "depreciation_amount": amount}, status=status.HTTP_201_CREATED)

    @decorators.action(detail=True, methods=("get",))
    def schedule(self, request, pk=None):
        asset = self.get_object()
        rows = asset.depreciations.select_related("journal_entry").order_by("period_date")
        accumulated = sum((row.amount for row in rows), start=0)
        return response.Response({
            "asset_id": asset.pk, "original_cost": asset.original_cost,
            "accumulated_depreciation": accumulated, "net_book_value": asset.original_cost - accumulated,
            "rows": AssetDepreciationSerializer(rows, many=True).data,
        })
