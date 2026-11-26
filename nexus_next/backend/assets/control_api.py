"""API for fixed-asset component, insurance and physical-count controls."""

from decimal import Decimal

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers, permissions, viewsets
from rest_framework.exceptions import PermissionDenied

from companies.access import accessible_branches, accessible_companies, can_access_branch, can_access_company, has_company_role
from .models import AssetComponent, AssetInsurancePolicy, AssetPhysicalInventory


class AssetComponentSerializer(serializers.ModelSerializer):
    class Meta:
        model = AssetComponent
        fields = (
            "id", "asset", "code", "name_ar", "original_cost", "salvage_value",
            "useful_life_months", "depreciation_method", "declining_rate_percentage",
            "production_units_total", "asset_account", "accumulated_depreciation_account",
            "depreciation_expense_account", "status", "created_at",
        )
        read_only_fields = ("status", "created_at")

    def validate(self, attrs):
        asset = attrs.get("asset", getattr(self.instance, "asset", None))
        if asset is None:
            raise serializers.ValidationError({"asset": "حدد الأصل المرتبط بالمكوّن."})
        from django.db.models import Sum
        existing = asset.components.exclude(pk=self.instance.pk) if self.instance else asset.components.all()
        allocated = existing.aggregate(total=Sum("original_cost"))["total"] or Decimal("0")
        new_cost = attrs.get("original_cost", getattr(self.instance, "original_cost", Decimal("0")))
        if allocated + new_cost > asset.original_cost:
            raise serializers.ValidationError({"original_cost": "مجموع تكلفة المكونات لا يجوز أن يتجاوز تكلفة الأصل الأم."})
        method = attrs.get("depreciation_method", getattr(self.instance, "depreciation_method", "straight_line"))
        rate = attrs.get("declining_rate_percentage", getattr(self.instance, "declining_rate_percentage", None))
        units = attrs.get("production_units_total", getattr(self.instance, "production_units_total", None))
        if method == "declining_balance" and (rate is None or rate <= 0 or rate > 100):
            raise serializers.ValidationError({"declining_rate_percentage": "أدخل معدلًا سنويًا أكبر من صفر وحتى 100%."})
        if method != "declining_balance" and rate is not None:
            raise serializers.ValidationError({"declining_rate_percentage": "المعدل يستخدم مع القسط المتناقص فقط."})
        if method == "units_of_production" and not units:
            raise serializers.ValidationError({"production_units_total": "حدد إجمالي وحدات الإنتاج."})
        if method != "units_of_production" and units is not None:
            raise serializers.ValidationError({"production_units_total": "الوحدات تستخدم مع طريقة وحدات الإنتاج فقط."})
        parent_accounts = {
            "asset_account": asset.asset_account_id,
            "accumulated_depreciation_account": asset.accumulated_depreciation_account_id,
            "depreciation_expense_account": asset.depreciation_expense_account_id,
        }
        for field, parent_account_id in parent_accounts.items():
            account = attrs.get(field, getattr(self.instance, field, None))
            if account and account.company_id != asset.company_id:
                raise serializers.ValidationError({field: "الحساب يجب أن يتبع شركة الأصل."})
            if account and account.pk != parent_account_id:
                raise serializers.ValidationError({field: "يجب أن تتطابق حسابات المكوّن مع حسابات الأصل الأم لمطابقة الأستاذ العام."})
        return attrs

    def create(self, validated_data):
        obj = AssetComponent(**validated_data)
        try:
            obj.full_clean()
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.message_dict if hasattr(exc, "message_dict") else exc.messages) from exc
        obj.save()
        return obj

    def update(self, instance, validated_data):
        for field, value in validated_data.items():
            setattr(instance, field, value)
        try:
            instance.full_clean()
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.message_dict if hasattr(exc, "message_dict") else exc.messages) from exc
        instance.save()
        return instance


class AssetInsurancePolicySerializer(serializers.ModelSerializer):
    class Meta:
        model = AssetInsurancePolicy
        fields = "__all__"
        read_only_fields = ("created_at",)

    def validate(self, attrs):
        if attrs.get("end_date", getattr(self.instance, "end_date", None)) < attrs.get("start_date", getattr(self.instance, "start_date", None)):
            raise serializers.ValidationError({"end_date": "تاريخ انتهاء الوثيقة لا يسبق تاريخ بدايتها."})
        if attrs.get("insured_value", Decimal("0")) < 0 or attrs.get("premium_amount", Decimal("0")) < 0:
            raise serializers.ValidationError("قيمة التأمين والقسط لا يمكن أن تكون سالبة.")
        return attrs


class AssetPhysicalInventorySerializer(serializers.ModelSerializer):
    class Meta:
        model = AssetPhysicalInventory
        fields = "__all__"
        read_only_fields = ("counted_by", "created_at")

    def validate(self, attrs):
        asset = attrs.get("asset", getattr(self.instance, "asset", None))
        count_date = attrs.get("count_date", getattr(self.instance, "count_date", None))
        if asset and count_date and count_date < asset.acquisition_date:
            raise serializers.ValidationError({"count_date": "تاريخ الجرد لا يسبق تاريخ اقتناء الأصل."})
        return attrs


class _AssetControlViewSet(viewsets.ModelViewSet):
    permission_classes = (permissions.IsAuthenticated,)
    model = None
    serializer_class = None

    def get_queryset(self):
        queryset = self.model.objects.filter(
            asset__company__in=accessible_companies(self.request.user),
            asset__branch__in=accessible_branches(self.request.user),
        ).select_related("asset", "asset__company", "asset__branch")
        asset_id = self.request.query_params.get("asset")
        if asset_id:
            if not asset_id.isdigit():
                raise PermissionDenied("معرّف الأصل غير صحيح.")
            queryset = queryset.filter(asset_id=int(asset_id))
        return queryset

    def _check_write(self, asset):
        if not can_access_company(self.request.user, asset.company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى شركة الأصل.")
        if not can_access_branch(self.request.user, asset.branch_id, asset.company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع الأصل.")
        if not has_company_role(self.request.user, asset.company_id, ("admin", "accountant")):
            raise PermissionDenied("تحتاج إلى دور مدير الشركة أو المحاسب.")

    def perform_create(self, serializer):
        asset = serializer.validated_data["asset"]
        self._check_write(asset)
        serializer.save()

    def perform_update(self, serializer):
        asset = serializer.validated_data.get("asset", serializer.instance.asset)
        self._check_write(asset)
        serializer.save()


class AssetComponentViewSet(_AssetControlViewSet):
    model = AssetComponent
    serializer_class = AssetComponentSerializer


class AssetInsurancePolicyViewSet(_AssetControlViewSet):
    model = AssetInsurancePolicy
    serializer_class = AssetInsurancePolicySerializer


class AssetPhysicalInventoryViewSet(_AssetControlViewSet):
    model = AssetPhysicalInventory
    serializer_class = AssetPhysicalInventorySerializer

    def perform_create(self, serializer):
        asset = serializer.validated_data["asset"]
        self._check_write(asset)
        serializer.save(
            counted_by=self.request.user,
            expected_location=asset.location,
            expected_custodian=asset.custodian,
        )
