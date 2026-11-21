from rest_framework import serializers
from .models import FixedAsset, AssetDepreciation


class FixedAssetSerializer(serializers.ModelSerializer):
    accumulated_depreciation = serializers.DecimalField(max_digits=20, decimal_places=4, read_only=True)
    net_book_value = serializers.DecimalField(max_digits=20, decimal_places=4, read_only=True)

    class Meta:
        model = FixedAsset
        fields = ("id", "company", "branch", "code", "name_ar", "description", "acquisition_date",
                  "depreciation_start_date", "original_cost", "salvage_value", "useful_life_months",
                  "depreciation_method", "asset_account", "accumulated_depreciation_account",
                  "depreciation_expense_account", "acquisition_counter_account", "disposal_gain_loss_account", "disposal_loss_account",
                  "acquisition_journal", "acquisition_treasury_transaction", "status", "disposed_at", "disposal_proceeds", "disposal_journal", "disposal_treasury_transaction",
                  "accumulated_depreciation", "net_book_value", "created_at")
        read_only_fields = ("acquisition_journal", "status", "disposed_at", "disposal_proceeds",
                            "disposal_journal", "disposal_treasury_transaction", "acquisition_treasury_transaction", "accumulated_depreciation", "net_book_value", "created_at")

    def validate(self, attrs):
        company = attrs.get("company", getattr(self.instance, "company", None))
        branch = attrs.get("branch", getattr(self.instance, "branch", None))
        if company and branch and branch.company_id != company.id:
            raise serializers.ValidationError({"branch": "الفرع لا يتبع الشركة."})
        for key in ("asset_account", "accumulated_depreciation_account", "depreciation_expense_account", "acquisition_counter_account", "disposal_gain_loss_account", "disposal_loss_account"):
            account = attrs.get(key, getattr(self.instance, key, None))
            if account and company and account.company_id != company.id:
                raise serializers.ValidationError({key: "الحساب لا يتبع الشركة."})
            if account and (not account.is_active or account.is_group or not account.allow_posting):
                raise serializers.ValidationError({key: "اختر حسابًا تفصيليًا نشطًا قابلًا للترحيل."})
            if account and key in ("asset_account", "accumulated_depreciation_account") and account.nature != "asset":
                raise serializers.ValidationError({key: "هذا الربط يتطلب حسابًا من طبيعة الأصول."})
            if account and key == "disposal_gain_loss_account" and account.nature != "revenue":
                raise serializers.ValidationError({key: "حساب أرباح بيع الأصول يجب أن يكون من طبيعة الإيرادات."})
            if account and key == "disposal_loss_account" and account.nature != "expense":
                raise serializers.ValidationError({key: "حساب خسائر بيع الأصول يجب أن يكون من طبيعة المصروفات."})
            if account and key == "depreciation_expense_account" and account.nature != "expense":
                raise serializers.ValidationError({key: "مصروف الإهلاك يجب أن يرتبط بحساب مصروفات."})
        cost = attrs.get("original_cost", getattr(self.instance, "original_cost", 0))
        salvage = attrs.get("salvage_value", getattr(self.instance, "salvage_value", 0))
        life = attrs.get("useful_life_months", getattr(self.instance, "useful_life_months", 0))
        if cost <= 0 or salvage < 0 or salvage > cost or life <= 0:
            raise serializers.ValidationError("راجع التكلفة والقيمة التخريدية والعمر الإنتاجي.")
        acquisition_date = attrs.get("acquisition_date", getattr(self.instance, "acquisition_date", None))
        depreciation_start = attrs.get("depreciation_start_date", getattr(self.instance, "depreciation_start_date", None))
        if acquisition_date and depreciation_start and depreciation_start < acquisition_date:
            raise serializers.ValidationError({"depreciation_start_date": "بداية الإهلاك لا تسبق تاريخ الاقتناء."})
        if not self.instance and not attrs.get("disposal_loss_account"):
            raise serializers.ValidationError({"disposal_loss_account": "حدد حساب خسائر استبعاد الأصول."})
        if self.instance and self.instance.acquisition_journal_id:
            raise serializers.ValidationError("لا يمكن تعديل أصل تم تسجيل قيد اقتنائه.")
        return attrs


class AssetDepreciationSerializer(serializers.ModelSerializer):
    class Meta:
        model = AssetDepreciation
        fields = ("id", "asset", "period_date", "amount", "is_partial_period", "journal_entry", "created_at")
