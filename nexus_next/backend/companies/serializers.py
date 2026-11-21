from rest_framework import serializers
from .models import Branch, Company, CompanyMembership, Currency, FiscalYear

class CurrencySerializer(serializers.ModelSerializer):
    class Meta:
        model = Currency
        fields = ("id", "code", "name_ar", "name_en", "symbol", "decimal_places", "is_active")

class CompanySerializer(serializers.ModelSerializer):
    base_currency_code = serializers.CharField(source="base_currency.code", read_only=True)

    class Meta:
        model = Company
        fields = (
            "id", "code", "name_ar", "name_en", "country", "activity",
            "base_currency", "base_currency_code", "tax_registration_number",
            "address", "phone", "email", "is_active", "created_at", "updated_at",
        )
        read_only_fields = ("created_at", "updated_at")

class BranchSerializer(serializers.ModelSerializer):
    company_code = serializers.CharField(source="company.code", read_only=True)

    class Meta:
        model = Branch
        fields = ("id", "company", "company_code", "code", "name_ar", "address", "phone", "is_head_office", "is_active", "created_at")
        read_only_fields = ("created_at",)

class FiscalYearSerializer(serializers.ModelSerializer):
    company_code = serializers.CharField(source="company.code", read_only=True)

    class Meta:
        model = FiscalYear
        fields = ("id", "company", "company_code", "name", "start_date", "end_date", "status", "created_at")
        read_only_fields = ("created_at",)

class CompanyMembershipSerializer(serializers.ModelSerializer):
    company_code = serializers.CharField(source="company.code", read_only=True)
    username = serializers.CharField(source="user.username", read_only=True)
    branches = serializers.PrimaryKeyRelatedField(queryset=Branch.objects.all(), many=True, required=False)

    class Meta:
        model = CompanyMembership
        fields = ("id", "company", "company_code", "user", "username", "role", "all_branches", "branches", "is_active", "created_at")
        read_only_fields = ("created_at",)

    def validate(self, attrs):
        company = attrs.get("company", self.instance.company if self.instance else None)
        branches = attrs.get("branches")
        if branches is None and self.instance:
            branches = list(self.instance.branches.all())
        if company and branches is not None and any(branch.company_id != company.pk for branch in branches):
            raise serializers.ValidationError({"branches": "كل الفروع المحددة يجب أن تتبع الشركة نفسها."})
        if self.instance and company and company.pk != self.instance.company_id:
            raise serializers.ValidationError({"company": "لا يمكن نقل عضوية المستخدم بين الشركات؛ أنشئ عضوية مستقلة."})
        if self.instance and "user" in attrs and attrs["user"].pk != self.instance.user_id:
            raise serializers.ValidationError({"user": "لا يمكن تغيير هوية المستخدم داخل عضوية قائمة."})
        role = attrs.get("role", self.instance.role if self.instance else CompanyMembership.Role.VIEWER)
        all_branches = attrs.get("all_branches", self.instance.all_branches if self.instance else False)
        if role == CompanyMembership.Role.ADMIN and not all_branches:
            raise serializers.ValidationError({"all_branches": "مدير الشركة يجب أن يمتلك نطاق الوصول إلى جميع الفروع."})
        return attrs
