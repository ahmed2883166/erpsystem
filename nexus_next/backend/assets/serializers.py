"""تحويل بيانات الأصول وجداول الإهلاك والتحقق من مدخلات API."""

from decimal import Decimal

from rest_framework import serializers
from .models import FixedAsset, AssetDepreciation


class FixedAssetSerializer(serializers.ModelSerializer):
    accumulated_depreciation = serializers.DecimalField(max_digits=20, decimal_places=4, read_only=True)
    net_book_value = serializers.DecimalField(max_digits=20, decimal_places=4, read_only=True)
    vendor_invoice_id = serializers.SerializerMethodField()
    vendor_invoice_status = serializers.SerializerMethodField()
    vendor_payments = serializers.SerializerMethodField()

    class Meta:
        model = FixedAsset
        fields = ("id", "company", "branch", "code", "name_ar", "category", "description", "location", "custodian", "serial_number", "warranty_end_date", "supplier_name", "supplier_invoice", "acquisition_date",
                  "depreciation_start_date", "original_cost", "acquisition_net_amount",
                  "nonrecoverable_tax_amount", "acquisition_tax_code", "salvage_value", "useful_life_months",
                  "depreciation_method", "asset_account", "accumulated_depreciation_account",
                  "depreciation_expense_account", "acquisition_counter_account", "disposal_gain_loss_account", "disposal_loss_account",
                  "acquisition_journal", "acquisition_treasury_transaction", "status", "disposed_at", "disposal_proceeds", "disposal_journal", "disposal_treasury_transaction",
                  "accumulated_depreciation", "net_book_value", "vendor_invoice_id", "vendor_invoice_status", "vendor_payments", "created_at")
        read_only_fields = ("acquisition_journal", "status", "disposed_at", "disposal_proceeds",
                            "disposal_journal", "disposal_treasury_transaction", "acquisition_treasury_transaction", "accumulated_depreciation", "net_book_value", "created_at")

    def get_vendor_invoice_id(self, obj):
        """Return the linked direct supplier invoice without failing for manual acquisitions."""
        invoice = getattr(obj, "vendor_invoice", None)
        return invoice.pk if invoice else None

    def get_vendor_invoice_status(self, obj):
        """Expose whether the direct supplier invoice is awaiting approval or already posted."""
        invoice = getattr(obj, "vendor_invoice", None)
        return invoice.status if invoice else None

    def get_vendor_payments(self, obj):
        """Expose linked supplier settlements for status tracking and approval-aware posting."""
        invoice = getattr(obj, "vendor_invoice", None)
        if not invoice:
            return []
        return [{
            "id": payment.pk,
            "payment_number": payment.payment_number,
            "payment_date": payment.payment_date,
            "amount": payment.amount,
            "status": payment.status,
            "journal_entry": payment.journal_entry_id,
        } for payment in invoice.payments.all()]

    def validate(self, attrs):
        """التحقق من صحة البيانات والعلاقات المرتبطة قبل حفظ السجل."""
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
            if account and key == "acquisition_counter_account" and account.nature not in ("asset", "liability"):
                raise serializers.ValidationError({key: "مقابل اقتناء الأصل يجب أن يكون حساب أصل أو التزام مثل البنك أو المورد."})
        asset_account = attrs.get("asset_account", getattr(self.instance, "asset_account", None))
        accumulated_account = attrs.get("accumulated_depreciation_account", getattr(self.instance, "accumulated_depreciation_account", None))
        counter_account = attrs.get("acquisition_counter_account", getattr(self.instance, "acquisition_counter_account", None))
        if asset_account and accumulated_account and asset_account.pk == accumulated_account.pk:
            raise serializers.ValidationError({"accumulated_depreciation_account": "يجب فصل حساب تكلفة الأصل عن حساب مجمع الإهلاك."})
        if asset_account and counter_account and asset_account.pk == counter_account.pk:
            raise serializers.ValidationError({"acquisition_counter_account": "لا يمكن استخدام حساب تكلفة الأصل كمقابل لاقتناء الأصل."})
        cost = attrs.get("original_cost", getattr(self.instance, "original_cost", 0))
        salvage = attrs.get("salvage_value", getattr(self.instance, "salvage_value", 0))
        life = attrs.get("useful_life_months", getattr(self.instance, "useful_life_months", 0))
        if cost <= 0 or salvage < 0 or salvage > cost or life <= 0:
            raise serializers.ValidationError("راجع التكلفة والقيمة التخريدية والعمر الإنتاجي.")
        net_amount = attrs.get("acquisition_net_amount", getattr(self.instance, "acquisition_net_amount", None))
        nonrecoverable_tax = attrs.get(
            "nonrecoverable_tax_amount",
            getattr(self.instance, "nonrecoverable_tax_amount", Decimal("0")),
        )
        tax_code = attrs.get("acquisition_tax_code", getattr(self.instance, "acquisition_tax_code", None))
        if nonrecoverable_tax < 0:
            raise serializers.ValidationError({"nonrecoverable_tax_amount": "قيمة الضريبة لا يمكن أن تكون سالبة."})
        if net_amount is not None and net_amount + nonrecoverable_tax != cost:
            raise serializers.ValidationError({
                "original_cost": "تكلفة الأصل يجب أن تساوي صافي الاقتناء مضافًا إليه الضريبة غير القابلة للخصم."
            })
        if nonrecoverable_tax and net_amount is None:
            raise serializers.ValidationError({"acquisition_net_amount": "أدخل صافي تكلفة الاقتناء."})
        if bool(tax_code) != bool(nonrecoverable_tax):
            raise serializers.ValidationError({
                "acquisition_tax_code": "حدد كود الضريبة غير القابلة للخصم مع قيمتها، أو اترك الاثنين دون قيمة."
            })
        if tax_code:
            acquisition_date = attrs.get("acquisition_date", getattr(self.instance, "acquisition_date", None))
            if (tax_code.company_id != company.pk or tax_code.country != company.country
                    or not tax_code.is_active or tax_code.input_tax_recoverable
                    or tax_code.category != "standard" or tax_code.rate <= 0
                    or tax_code.effective_from > acquisition_date
                    or (tax_code.effective_to and tax_code.effective_to < acquisition_date)):
                raise serializers.ValidationError({
                    "acquisition_tax_code": "اختر كود ضريبة قياسيًا نشطًا وغير قابل للخصم وساريًا بتاريخ الاقتناء."
                })
            expected_tax = (net_amount * tax_code.rate / Decimal("100")).quantize(Decimal("0.0001"))
            if expected_tax != nonrecoverable_tax:
                raise serializers.ValidationError({
                    "nonrecoverable_tax_amount": "قيمة الضريبة لا تطابق صافي الاقتناء ونسبة كود الضريبة."
                })
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
