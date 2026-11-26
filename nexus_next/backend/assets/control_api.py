"""API for fixed-asset component, insurance and physical-count controls."""

from decimal import Decimal

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers, permissions, viewsets
from rest_framework.exceptions import PermissionDenied

from companies.access import accessible_companies, can_access_branch, can_access_company, has_company_role
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
        if asset.acquisition_journal_id and self.instance is None:
            # Components are a subledger allocation; the sum of component costs
            # may not exceed the original capitalized asset cost.
            current = asset.components.aggregate_total if hasattr(asset.components, "aggregate_total") else None
            if current is None:
                from django.db.models import Sum
                current = asset.components.aggregate(total=Sum("original_cost"))["total"] or Decimal("0")
            new_cost = attrs.get("original_cost", Decimal("0"))
            if current + new_cost > asset.original_cost:
                raise serializers.ValidationError({"original_cost": "مجموع تكلفة المكونات لا يجوز أن يتجاوز تكلفة الأصل الأم."})
        method = attrs.get("depreciation_method", AssetComponent._meta.get_field("depreciation_method").default)
        rate = attrs.get("declining_rate_percentage")
        units = attrs.get("production_units_total")
        if method == "declining_balance" and (rate is None or rate <= 0 or rate > 100):
            raise serializers.ValidationError({"declining_rate_percentage": "أدخل معدلًا سنويًا أكبر من صفر وحتى 100%."})
        if method != "declining_balance" and rate is not None:
            raise serializers.ValidationError({"declining_rate_percentage": "المعدل يستخدم مع القسط المتناقص فقط."})
        if method == "units_of_production" and not units:
            raise serializers.ValidationError({"production_units_total": "حدد إجمالي وحدات الإنتاج."})
        if method != "units_of_production" and units is not None:
            raise serializers.ValidationError({"production_units_total": "الوحدات تستخدم مع طريقة وحدات الإنتاج فقط."})
        for field in ("asset_account", "accumulated_depreciation_account", "depreciation_expense_account"):
            account = attrs.get(field, getattr(self.instance, field, None))
            if account and account.company_id != asset.company_id:
                raise serializers.ValidationError({field: "الحساب يجب أن يتبع شركة الأصل."})
        return attrs

    def create(self, validated_data):
        obj = AssetComponent(**validated_data)
        try:
            obj.full_clean()
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.message_dict if hasattr(exc, "message_dict") else exc.messages) from exc
        obj.save()
        return obj


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
        return self.model.objects.filter(asset__company__in=accessible_companies(self.request.user)).select_related("asset", "asset__company", "asset__branch")

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
        self._check_write(serializer.instance.asset)
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
        serializer.save(counted_by=self.request.user)
