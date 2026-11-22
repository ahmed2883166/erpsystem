"""تحويل نماذج المحاسبة إلى صيغ API والتحقق من صحة مدخلات المستخدم.
تتضمن بعض الحقول المقروءة فقط بيانات مساعدة لعرض الحسابات والقيود.
"""

from django.db import transaction
from django.db.models import Q
from companies.models import FiscalYear
from rest_framework import serializers
from .models import AccountRole, ChartOfAccount, CostCenter, JournalEntry, JournalLine, TaxCode


class ChartOfAccountSerializer(serializers.ModelSerializer):
    parent_code = serializers.CharField(source="parent.code", read_only=True)

    class Meta:
        model = ChartOfAccount
        fields = ("id", "company", "code", "name_ar", "name_en", "parent", "parent_code",
                  "nature", "is_group", "allow_posting", "is_active", "created_at")
        read_only_fields = ("created_at",)

    def validate(self, attrs):
        """التحقق من صحة المدخلات واتساق العلاقات قبل قبول بيانات المستند."""
        company = attrs.get("company", getattr(self.instance, "company", None))
        parent = attrs.get("parent", getattr(self.instance, "parent", None))
        if parent and company and parent.company_id != company.id:
            raise serializers.ValidationError({"parent": "الحساب الأب يجب أن يتبع نفس الشركة."})
        if parent and not parent.is_group:
            raise serializers.ValidationError({"parent": "الحساب الأب يجب أن يكون تجميعياً."})
        if self.instance and parent and parent.pk == self.instance.pk:
            raise serializers.ValidationError({"parent": "لا يمكن ربط الحساب بنفسه."})
        if attrs.get("is_group", getattr(self.instance, "is_group", False)) and attrs.get(
            "allow_posting", getattr(self.instance, "allow_posting", True)
        ):
            raise serializers.ValidationError({"allow_posting": "الحساب التجميعي لا يقبل الترحيل."})
        return attrs


class AccountRoleSerializer(serializers.ModelSerializer):
    account_code = serializers.CharField(source="account.code", read_only=True)
    ROLE_NATURES = {
        "cash": {"asset"}, "bank": {"asset"}, "accounts_receivable": {"asset"},
        "inventory": {"asset"}, "vat_input": {"asset"},
        "accounts_payable": {"liability"}, "vat_output": {"liability"}, "vat_payable": {"liability"},
        "employee_insurance_payable": {"liability"}, "employer_insurance_payable": {"liability"},
        "payroll_payable": {"liability"}, "goods_received_not_invoiced": {"liability"},
        "inter_branch_clearing": {"liability"},
        "realized_fx_gain": {"revenue"}, "unrealized_fx_gain": {"revenue"},
        "realized_fx_loss": {"expense"}, "unrealized_fx_loss": {"expense"},
        "sales": {"revenue"}, "sales_returns": {"revenue"}, "inventory_adjustment_gain": {"revenue"},
        "cogs": {"expense"}, "operating_expense": {"expense"}, "payroll_expense": {"expense"},
        "employer_insurance_expense": {"expense"}, "inventory_adjustment_loss": {"expense"},
        "retained_earnings": {"equity"},
    }
    class Meta:
        model = AccountRole
        fields = ("id", "company", "role", "account", "account_code")

    def validate(self, attrs):
        """التحقق من صحة المدخلات واتساق العلاقات قبل قبول بيانات المستند."""
        company = attrs.get("company", getattr(self.instance, "company", None))
        account = attrs.get("account", getattr(self.instance, "account", None))
        if self.instance and "company" in attrs and company.pk != self.instance.company_id:
            raise serializers.ValidationError({"company": "لا يمكن نقل الدور المحاسبي إلى شركة أخرى؛ أنشئ ربطًا مستقلًا للشركة الأخرى."})
        if self.instance and "role" in attrs and attrs["role"] != self.instance.role:
            raise serializers.ValidationError({"role": "هوية الدور المحاسبي ثابتة؛ غيّر الحساب المرتبط فقط."})
        if company and account and account.company_id != company.id:
            raise serializers.ValidationError({"account": "الحساب لا يتبع نفس الشركة."})
        if account and (account.is_group or not account.allow_posting or not account.is_active):
            raise serializers.ValidationError({"account": "اختر حسابًا تفصيليًا نشطًا قابلًا للترحيل."})
        role = attrs.get("role", getattr(self.instance, "role", None))
        expected_natures = self.ROLE_NATURES.get(role)
        if account and expected_natures and account.nature not in expected_natures:
            raise serializers.ValidationError({"account": "طبيعة الحساب لا تتوافق مع الدور المحاسبي المختار."})
        return attrs


class CostCenterSerializer(serializers.ModelSerializer):
    class Meta:
        model = CostCenter
        fields = ("id", "company", "code", "name_ar", "parent", "is_active")

    def validate(self, attrs):
        """التحقق من صحة المدخلات واتساق العلاقات قبل قبول بيانات المستند."""
        company = attrs.get("company", getattr(self.instance, "company", None))
        parent = attrs.get("parent", getattr(self.instance, "parent", None))
        if parent and company and parent.company_id != company.id:
            raise serializers.ValidationError({"parent": "مركز التكلفة الأب يجب أن يتبع نفس الشركة."})
        if self.instance and parent:
            visited = set()
            node = parent
            while node is not None:
                if node.pk == self.instance.pk:
                    raise serializers.ValidationError({"parent": "لا يمكن ربط مركز التكلفة بأحد أبنائه؛ سيؤدي ذلك إلى دورة في الهيكل."})
                if node.pk in visited:
                    raise serializers.ValidationError({"parent": "يوجد تسلسل أبوة دائري في مراكز التكلفة؛ راجع الهيكل قبل التعديل."})
                visited.add(node.pk)
                node = node.parent
        return attrs


class JournalLineSerializer(serializers.ModelSerializer):
    account_code = serializers.CharField(source="account.code", read_only=True)
    account_name = serializers.CharField(source="account.name_ar", read_only=True)

    class Meta:
        model = JournalLine
        fields = ("id", "account", "account_code", "account_name", "description",
                  "debit", "credit", "transaction_currency", "transaction_amount",
                  "exchange_rate", "exchange_rate_date", "cost_center")
        read_only_fields = ("id", "account_code", "account_name")

    def validate(self, attrs):
        """التحقق من صحة المدخلات واتساق العلاقات قبل قبول بيانات المستند."""
        debit = attrs.get("debit", getattr(self.instance, "debit", 0))
        credit = attrs.get("credit", getattr(self.instance, "credit", 0))
        if debit < 0 or credit < 0 or ((debit > 0) == (credit > 0)):
            raise serializers.ValidationError("يجب إدخال قيمة مدين أو دائن فقط في السطر.")
        snapshot = (
            attrs.get("transaction_currency", getattr(self.instance, "transaction_currency", None)),
            attrs.get("transaction_amount", getattr(self.instance, "transaction_amount", None)),
            attrs.get("exchange_rate", getattr(self.instance, "exchange_rate", None)),
            attrs.get("exchange_rate_date", getattr(self.instance, "exchange_rate_date", None)),
        )
        if any(value is not None for value in snapshot) and any(value is None for value in snapshot):
            raise serializers.ValidationError("بيانات العملة الأجنبية غير مكتملة؛ أدخل العملة والمبلغ والسعر وتاريخ السعر.")
        if snapshot[1] is not None and snapshot[1] <= 0:
            raise serializers.ValidationError({"transaction_amount": "مبلغ العملة يجب أن يكون أكبر من صفر."})
        if snapshot[2] is not None and snapshot[2] <= 0:
            raise serializers.ValidationError({"exchange_rate": "سعر الصرف يجب أن يكون أكبر من صفر."})
        if snapshot[0] is not None and not snapshot[0].is_active:
            raise serializers.ValidationError({"transaction_currency": "العملة المختارة موقوفة."})
        return attrs


class JournalEntrySerializer(serializers.ModelSerializer):
    lines = JournalLineSerializer(many=True)
    branch_code = serializers.CharField(source="branch.code", read_only=True)
    fiscal_year_name = serializers.CharField(source="fiscal_year.name", read_only=True)
    created_by_name = serializers.CharField(source="created_by.display_name", read_only=True, default="")

    class Meta:
        model = JournalEntry
        fields = ("id", "company", "branch", "branch_code", "fiscal_year", "fiscal_year_name",
                  "entry_number", "entry_date", "description", "source_type", "status",
                  "reversal_of", "created_by", "created_by_name", "created_at", "posted_at", "lines")
        read_only_fields = ("status", "created_by", "created_at", "posted_at")

    def validate(self, attrs):
        """التحقق من صحة المدخلات واتساق العلاقات قبل قبول بيانات المستند."""
        company = attrs.get("company", getattr(self.instance, "company", None))
        branch = attrs.get("branch", getattr(self.instance, "branch", None))
        year = attrs.get("fiscal_year", getattr(self.instance, "fiscal_year", None))
        if branch and company and branch.company_id != company.id:
            raise serializers.ValidationError({"branch": "الفرع لا يتبع الشركة المحددة."})
        if year and company and year.company_id != company.id:
            raise serializers.ValidationError({"fiscal_year": "السنة المالية لا تتبع الشركة المحددة."})
        if self.instance and self.instance.status != JournalEntry.Status.DRAFT:
            raise serializers.ValidationError("لا يمكن تعديل قيد غير مسودة.")
        lines = attrs.get("lines")
        if lines is not None:
            if len(lines) < 2:
                raise serializers.ValidationError({"lines": "القيد يحتاج سطرين على الأقل."})
            for line in lines:
                account = line["account"]
                if company and account.company_id != company.id:
                    raise serializers.ValidationError({"lines": "كل الحسابات يجب أن تتبع نفس الشركة."})
                if not account.is_active or account.is_group or not account.allow_posting:
                    raise serializers.ValidationError({"lines": f"الحساب {account.code} غير نشط أو غير قابل للترحيل."})
                center = line.get("cost_center")
                if center and company and center.company_id != company.id:
                    raise serializers.ValidationError({"lines": "مركز التكلفة لا يتبع نفس الشركة."})
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        """إنشاء المستند وبنوده داخل معاملة ذرية لضمان اتساق البيانات."""
        lines = validated_data.pop("lines")
        fiscal_year = FiscalYear.objects.select_for_update().get(pk=validated_data["fiscal_year"].pk)
        if fiscal_year.status != FiscalYear.Status.OPEN:
            raise serializers.ValidationError({"fiscal_year": "لا يمكن إنشاء قيد داخل سنة مالية مغلقة."})
        request = self.context.get("request")
        if request and request.user.is_authenticated:
            validated_data["created_by"] = request.user
        entry = JournalEntry.objects.create(**validated_data)
        JournalLine.objects.bulk_create([JournalLine(entry=entry, **line) for line in lines])
        return entry

    @transaction.atomic
    def update(self, instance, validated_data):
        """تحديث المستند وبنوده مع حماية القيود والمستندات المرحلة."""
        lines = validated_data.pop("lines", None)
        target_year = validated_data.get("fiscal_year", instance.fiscal_year)
        fiscal_year = FiscalYear.objects.select_for_update().get(pk=target_year.pk)
        if fiscal_year.status != FiscalYear.Status.OPEN:
            raise serializers.ValidationError({"fiscal_year": "لا يمكن حفظ قيد داخل سنة مالية مغلقة."})
        for key, value in validated_data.items():
            setattr(instance, key, value)
        instance.save()
        if lines is not None:
            instance.lines.all().delete()
            JournalLine.objects.bulk_create([JournalLine(entry=instance, **line) for line in lines])
        return instance



class TaxCodeSerializer(serializers.ModelSerializer):
    company_name = serializers.CharField(source="company.name_ar", read_only=True)

    class Meta:
        model = TaxCode
        fields = ("id", "company", "company_name", "country", "code", "name_ar", "category",
                  "rate", "effective_from", "effective_to", "input_tax_recoverable",
                  "is_active", "created_at")
        read_only_fields = ("created_at",)

    def validate(self, attrs):
        """التحقق من صحة المدخلات واتساق العلاقات قبل قبول بيانات المستند."""
        company = attrs.get("company", getattr(self.instance, "company", None))
        country = attrs.get("country", getattr(self.instance, "country", None))
        category = attrs.get("category", getattr(self.instance, "category", TaxCode.Category.STANDARD))
        rate = attrs.get("rate", getattr(self.instance, "rate", 0))
        effective_from = attrs.get("effective_from", getattr(self.instance, "effective_from", None))
        effective_to = attrs.get("effective_to", getattr(self.instance, "effective_to", None))
        if company and country and company.country != country:
            raise serializers.ValidationError({"country": "بلد الضريبة يجب أن يطابق بلد الشركة."})
        if effective_from and effective_to and effective_to < effective_from:
            raise serializers.ValidationError({"effective_to": "تاريخ انتهاء الضريبة يجب ألا يسبق تاريخ بدايتها."})
        if category in (TaxCode.Category.ZERO_RATED, TaxCode.Category.EXEMPT, TaxCode.Category.OUT_OF_SCOPE) and rate != 0:
            raise serializers.ValidationError({"rate": "هذه الفئة يجب أن تكون بنسبة صفر."})
        if category == TaxCode.Category.STANDARD and rate <= 0:
            raise serializers.ValidationError({"rate": "أدخل نسبة موجبة للفئة القياسية."})
        code = attrs.get("code", getattr(self.instance, "code", None))
        if company and code and effective_from:
            overlapping = TaxCode.objects.filter(company=company, code=code)
            if self.instance:
                overlapping = overlapping.exclude(pk=self.instance.pk)
            overlapping = overlapping.filter(
                Q(effective_to__isnull=True) | Q(effective_to__gte=effective_from)
            )
            if effective_to:
                overlapping = overlapping.filter(effective_from__lte=effective_to)
            if overlapping.exists():
                raise serializers.ValidationError({
                    "effective_from": "فترة السريان تتداخل مع إصدار آخر لنفس كود الضريبة؛ أغلق الفترة السابقة أو اختر فترة غير متداخلة."
                })
        return attrs
