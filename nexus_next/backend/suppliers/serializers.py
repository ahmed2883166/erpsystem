from rest_framework import serializers

from .models import Supplier


class SupplierSerializer(serializers.ModelSerializer):
    company_code = serializers.CharField(source="company.code", read_only=True)

    class Meta:
        model = Supplier
        fields = (
            "id", "company", "company_code", "code", "supplier_type",
            "name_ar", "name_en", "tax_number", "phone", "mobile", "email",
            "address", "credit_limit", "payment_terms_days", "notes",
            "is_active", "created_at", "updated_at",
        )
        read_only_fields = ("created_at", "updated_at")

    def validate_code(self, value):
        return value.strip().upper()

    def validate_name_ar(self, value):
        if not value.strip():
            raise serializers.ValidationError("اسم المورد مطلوب.")
        return value.strip()

    def validate_credit_limit(self, value):
        if value < 0:
            raise serializers.ValidationError("حد الائتمان لا يمكن أن يكون سالبًا.")
        return value
