from rest_framework import serializers

from .models import Customer


class CustomerSerializer(serializers.ModelSerializer):
    company_code = serializers.CharField(source="company.code", read_only=True)

    class Meta:
        model = Customer
        fields = (
            "id", "company", "company_code", "code", "customer_type",
            "name_ar", "name_en", "tax_number", "phone", "mobile", "email",
            "address", "credit_limit", "payment_terms_days", "default_discount_percent",
            "discount_approval_limit", "notes",
            "is_active", "created_at", "updated_at",
        )
        read_only_fields = ("created_at", "updated_at")

    def validate_code(self, value):
        """التحقق من صحة البيانات والعلاقات المرتبطة قبل حفظ السجل."""
        return value.strip().upper()

    def validate_name_ar(self, value):
        """التحقق من صحة البيانات والعلاقات المرتبطة قبل حفظ السجل."""
        if not value.strip():
            raise serializers.ValidationError("اسم العميل مطلوب.")
        return value.strip()

    def validate(self, attrs):
        attrs = super().validate(attrs)
        default_discount = attrs.get("default_discount_percent", getattr(self.instance, "default_discount_percent", 0))
        approval_limit = attrs.get("discount_approval_limit", getattr(self.instance, "discount_approval_limit", None))
        if approval_limit is not None and default_discount > approval_limit:
            raise serializers.ValidationError({"default_discount_percent": "الخصم الافتراضي لا يمكن أن يتجاوز حد الخصم دون موافقة المدير."})
        return attrs

    def validate_credit_limit(self, value):
        """التحقق من صحة البيانات والعلاقات المرتبطة قبل حفظ السجل."""
        if value < 0:
            raise serializers.ValidationError("حد الائتمان لا يمكن أن يكون سالبًا.")
        return value
