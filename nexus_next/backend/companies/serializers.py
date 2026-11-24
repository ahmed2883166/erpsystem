from django.db import transaction
from rest_framework import serializers
from .models import Branch, Company, CompanyMembership, Currency, ExchangeRate, FiscalYear

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

    @transaction.atomic
    def create(self, validated_data):
        """إنشاء الشركة وفرعها الرئيسي كوحدة واحدة حتى لا تُترك شركة بلا فرع تشغيلي."""
        company = super().create(validated_data)
        Branch.objects.get_or_create(
            company=company,
            code="MAIN",
            defaults={
                "name_ar": "الفرع الرئيسي",
                "is_head_office": True,
                "is_active": True,
            },
        )
        return company

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
        read_only_fields = ("created_at", "is_head_office")

    def validate(self, attrs):
        """منع تغيير هوية الفرع والتحقق من تفرد الكود والفرع الرئيسي قبل الحفظ."""
        requested_head_office = self.initial_data.get("is_head_office")
        if str(requested_head_office).strip().lower() in {"true", "1", "yes", "on"}:
            raise serializers.ValidationError({
                "is_head_office": "تحديد الفرع الرئيسي يتم تلقائيًا عند تأسيس الشركة ولا يمكن تعيينه من شاشة الفروع."
            })
        company = attrs.get("company", self.instance.company if self.instance else None)
        code = attrs.get("code", self.instance.code if self.instance else None)
        if self.instance:
            if "company" in attrs and attrs["company"].pk != self.instance.company_id:
                raise serializers.ValidationError({"company": "لا يمكن نقل فرع قائم إلى شركة أخرى."})
            if "code" in attrs and attrs["code"] != self.instance.code:
                raise serializers.ValidationError({"code": "كود الفرع ثابت بعد إنشائه حفاظًا على المراجع والسجلات."})

        if company and code:
            duplicate_code = Branch.objects.filter(company=company, code=code)
            if self.instance:
                duplicate_code = duplicate_code.exclude(pk=self.instance.pk)
            if duplicate_code.exists():
                raise serializers.ValidationError({"code": "كود الفرع مستخدم بالفعل داخل هذه الشركة."})

        is_head_office = attrs.get(
            "is_head_office", self.instance.is_head_office if self.instance else False
        )
        if company and is_head_office:
            another_head_office = Branch.objects.filter(company=company, is_head_office=True)
            if self.instance:
                another_head_office = another_head_office.exclude(pk=self.instance.pk)
            if another_head_office.exists():
                raise serializers.ValidationError({
                    "is_head_office": "يوجد فرع رئيسي محدد بالفعل لهذه الشركة."
                })
        return attrs


class FiscalYearSerializer(serializers.ModelSerializer):
    company_code = serializers.CharField(source="company.code", read_only=True)

    class Meta:
        model = FiscalYear
        fields = ("id", "company", "company_code", "name", "start_date", "end_date", "status", "created_at")
        read_only_fields = ("created_at", "status")

    def validate(self, attrs):
        """حماية ارتباط السنة المالية والتحقق من ترتيب حدودها حتى في طلبات PATCH."""
        if self.instance and "company" in attrs and attrs["company"].pk != self.instance.company_id:
            raise serializers.ValidationError({
                "company": "لا يمكن نقل سنة مالية قائمة إلى شركة أخرى؛ أنشئ سنة مستقلة للشركة المطلوبة."
            })

        company = attrs.get("company", self.instance.company if self.instance else None)
        name = attrs.get("name", self.instance.name if self.instance else None)
        if company and name:
            duplicate_name = FiscalYear.objects.filter(company=company, name=name)
            if self.instance:
                duplicate_name = duplicate_name.exclude(pk=self.instance.pk)
            if duplicate_name.exists():
                raise serializers.ValidationError({"name": "اسم السنة المالية مستخدم بالفعل لهذه الشركة."})

        start_date = attrs.get("start_date", self.instance.start_date if self.instance else None)
        end_date = attrs.get("end_date", self.instance.end_date if self.instance else None)
        if start_date and end_date and end_date < start_date:
            raise serializers.ValidationError({
                "end_date": "تاريخ نهاية السنة المالية يجب أن يكون في أو بعد تاريخ البداية."
            })
        if company and start_date and end_date:
            overlapping_years = FiscalYear.objects.filter(
                company=company, start_date__lte=end_date, end_date__gte=start_date
            )
            if self.instance:
                overlapping_years = overlapping_years.exclude(pk=self.instance.pk)
            if overlapping_years.exists():
                raise serializers.ValidationError({
                    "start_date": "فترة السنة المالية تتداخل مع سنة أخرى للشركة؛ يجب فصل الفترات دون تداخل."
                })
        return attrs

class CompanyMembershipSerializer(serializers.ModelSerializer):
    company_code = serializers.CharField(source="company.code", read_only=True)
    username = serializers.CharField(source="user.username", read_only=True)
    display_name = serializers.CharField(source="user.display_name", read_only=True)
    branches = serializers.PrimaryKeyRelatedField(queryset=Branch.objects.all(), many=True, required=False)

    class Meta:
        model = CompanyMembership
        fields = ("id", "company", "company_code", "user", "username", "display_name", "role", "all_branches", "branches", "is_active", "created_at")
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



class ExchangeRateSerializer(serializers.ModelSerializer):
    company_code = serializers.CharField(source="company.code", read_only=True)
    currency_code = serializers.CharField(source="currency.code", read_only=True)
    base_currency_code = serializers.CharField(source="company.base_currency.code", read_only=True)

    class Meta:
        model = ExchangeRate
        fields = ("id", "company", "company_code", "currency", "currency_code",
                  "base_currency_code", "rate_date", "rate", "source", "notes", "created_at")
        read_only_fields = ("created_at",)

    def validate(self, attrs):
        company = attrs.get("company", self.instance.company if self.instance else None)
        currency = attrs.get("currency", self.instance.currency if self.instance else None)
        rate_date = attrs.get("rate_date", self.instance.rate_date if self.instance else None)
        rate = attrs.get("rate", self.instance.rate if self.instance else None)
        if company and currency:
            if currency.code == company.base_currency.code:
                raise serializers.ValidationError({"currency": "لا يلزم تسجيل سعر صرف للعملة الأساسية للشركة."})
            if not currency.is_active:
                raise serializers.ValidationError({"currency": "لا يمكن استخدام عملة موقوفة."})
        if rate is not None and rate <= 0:
            raise serializers.ValidationError({"rate": "سعر الصرف يجب أن يكون أكبر من صفر."})
        if company and currency and rate_date and ExchangeRate.objects.filter(
            company=company, currency=currency, rate_date=rate_date
        ).exists():
            raise serializers.ValidationError({"rate_date": "يوجد سعر صرف مسجل لهذه العملة في هذا التاريخ؛ لا يمكن استبداله."})
        if self.instance:
            raise serializers.ValidationError("سجل سعر الصرف تاريخي؛ أضف سجلًا جديدًا بتاريخ مستقل بدل تعديل سجل سابق.")
        return attrs
