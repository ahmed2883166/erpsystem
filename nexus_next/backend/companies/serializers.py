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

    def validate(self, attrs):
        """حماية مفاتيح تأسيس الشركة من التغيير بعد إنشائها حفاظًا على سلامة الحسابات والضرائب."""
        if self.instance:
            protected = ("code", "country", "activity", "base_currency")
            changed = [
                field for field in protected
                if field in attrs and getattr(self.instance, field + ("_id" if field == "base_currency" else "")) != (
                    attrs[field].pk if field == "base_currency" else attrs[field]
                )
            ]
            if changed:
                labels = {
                    "code": "كود الشركة",
                    "country": "الدولة",
                    "activity": "النشاط",
                    "base_currency": "العملة الأساسية",
                }
                raise serializers.ValidationError({
                    field: f"لا يمكن تغيير {labels[field]} بعد إنشاء الشركة؛ يلزم إجراء تحويل تأسيسي مستقل ومراجع."
                    for field in changed
                })
        return attrs

class BranchSerializer(serializers.ModelSerializer):
    company_code = serializers.CharField(source="company.code", read_only=True)

    class Meta:
        model = Branch
        fields = ("id", "company", "company_code", "code", "name_ar", "address", "phone", "is_head_office", "is_active", "created_at")
        read_only_fields = ("created_at",)

    def validate(self, attrs):
        """منع تغيير هوية الفرع أو نقله بعد إنشائه مع السماح بتحديث بياناته التشغيلية."""
        if self.instance:
            if "company" in attrs and attrs["company"].pk != self.instance.company_id:
                raise serializers.ValidationError({"company": "لا يمكن نقل فرع قائم إلى شركة أخرى."})
            if "code" in attrs and attrs["code"] != self.instance.code:
                raise serializers.ValidationError({"code": "كود الفرع ثابت بعد إنشائه حفاظًا على المراجع والسجلات."})
        return attrs


class FiscalYearSerializer(serializers.ModelSerializer):
    company_code = serializers.CharField(source="company.code", read_only=True)

    class Meta:
        model = FiscalYear
        fields = ("id", "company", "company_code", "name", "start_date", "end_date", "status", "created_at")
        read_only_fields = ("created_at",)

    def validate(self, attrs):
        """حماية ارتباط السنة المالية والتحقق من ترتيب حدودها حتى في طلبات PATCH."""
        if self.instance and "company" in attrs and attrs["company"].pk != self.instance.company_id:
            raise serializers.ValidationError({
                "company": "لا يمكن نقل سنة مالية قائمة إلى شركة أخرى؛ أنشئ سنة مستقلة للشركة المطلوبة."
            })

        start_date = attrs.get("start_date", self.instance.start_date if self.instance else None)
        end_date = attrs.get("end_date", self.instance.end_date if self.instance else None)
        if start_date and end_date and end_date < start_date:
            raise serializers.ValidationError({
                "end_date": "تاريخ نهاية السنة المالية يجب أن يكون في أو بعد تاريخ البداية."
            })
        return attrs

class CompanyMembershipSerializer(serializers.ModelSerializer):
    company_code = serializers.CharField(source="company.code", read_only=True)
    username = serializers.CharField(source="user.username", read_only=True)
    branches = serializers.PrimaryKeyRelatedField(queryset=Branch.objects.all(), many=True, required=False)

    class Meta:
        model = CompanyMembership
        fields = ("id", "company", "company_code", "user", "username", "role", "all_branches", "branches", "is_active", "created_at")
        read_only_fields = ("created_at",)

    def validate_user(self, user):
        """التحقق من صحة البيانات والعلاقات المرتبطة قبل حفظ السجل."""
        if not user.is_active:
            raise serializers.ValidationError("لا يمكن إضافة مستخدم غير نشط إلى الشركة.")
        return user

    def validate(self, attrs):
        """التحقق من صحة البيانات والعلاقات المرتبطة قبل حفظ السجل."""
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
