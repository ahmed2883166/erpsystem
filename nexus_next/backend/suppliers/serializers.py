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
        """التحقق من صحة البيانات والعلاقات المرتبطة قبل حفظ السجل."""
        return value.strip().upper()

    def validate_name_ar(self, value):
        """التحقق من صحة البيانات والعلاقات المرتبطة قبل حفظ السجل."""
        if not value.strip():
            raise serializers.ValidationError("اسم المورد مطلوب.")
        return value.strip()

    def validate_tax_number(self, value):
        """منع تكرار الرقم الضريبي داخل الشركة مع تجاهل اختلاف حالة الأحرف والمسافات."""
        value = value.strip()
        if not value:
            return value
        company = self.initial_data.get("company")
        if company is not None:
            try:
                company_id = int(company)
            except (TypeError, ValueError):
                company_id = None
        else:
            company_id = getattr(self.instance, "company_id", None)
        if company_id is None and self.instance is None:
            return value
        duplicates = Supplier.objects.filter(
            company_id=company_id,
            tax_number__iexact=value,
        )
        if self.instance is not None:
            duplicates = duplicates.exclude(pk=self.instance.pk)
        if duplicates.exists():
            raise serializers.ValidationError("الرقم الضريبي مستخدم بالفعل لمورد آخر داخل الشركة.")
        return value

    def validate_credit_limit(self, value):
        """التحقق من صحة البيانات والعلاقات المرتبطة قبل حفظ السجل."""
        if value < 0:
            raise serializers.ValidationError("حد الائتمان لا يمكن أن يكون سالبًا.")
        return value
