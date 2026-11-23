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


from .models import CustomerDiscountRule


class CustomerDiscountRuleSerializer(serializers.ModelSerializer):
    customer_name = serializers.CharField(source="customer.name_ar", read_only=True, allow_null=True)
    product_name = serializers.CharField(source="product.name_ar", read_only=True, allow_null=True)
    category_name = serializers.CharField(source="category.name_ar", read_only=True, allow_null=True)

    class Meta:
        model = CustomerDiscountRule
        fields = (
            "id", "company", "customer", "customer_name", "customer_type",
            "product", "product_name", "category", "category_name",
            "min_quantity", "min_invoice_amount", "discount_percent", "priority",
            "effective_from", "effective_to", "is_active", "notes",
            "created_at", "updated_at",
        )
        read_only_fields = ("created_at", "updated_at")

    def validate(self, attrs):
        attrs = super().validate(attrs)
        company = attrs.get("company", self.instance.company if self.instance else None)
        customer = attrs.get("customer", self.instance.customer if self.instance else None)
        product = attrs.get("product", self.instance.product if self.instance else None)
        category = attrs.get("category", self.instance.category if self.instance else None)
        if self.instance and company.pk != self.instance.company_id:
            raise serializers.ValidationError({"company": "لا يمكن نقل قاعدة الخصم إلى شركة أخرى."})
        for field, value in (("customer", customer), ("product", product), ("category", category)):
            if value is not None and value.company_id != company.pk:
                raise serializers.ValidationError({field: "يجب أن يتبع السجل الشركة المحددة."})
        if attrs.get("effective_to", getattr(self.instance, "effective_to", None)) is not None:
            end = attrs.get("effective_to", self.instance.effective_to if self.instance else None)
            start = attrs.get("effective_from", self.instance.effective_from if self.instance else None)
            if start and end < start:
                raise serializers.ValidationError({"effective_to": "تاريخ النهاية يجب ألا يسبق تاريخ البداية."})
        return attrs
