from decimal import Decimal, InvalidOperation

from rest_framework import serializers

from .models import (
    AttendanceRecord, Department, Employee, EmployeeComponent, EmployeeLoan, EmployeeSalaryHistory, LoanInstallment,
    JobPosition, LeaveRequest, PayrollComponent, PayrollCycle, PayrollResult,
    PayrollRuleSet,
)


class DepartmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Department
        fields = "__all__"

    def validate(self, attrs):
        company = attrs.get("company", getattr(self.instance, "company", None))
        branch = attrs.get("branch", getattr(self.instance, "branch", None))
        if branch and company and branch.company_id != company.pk:
            raise serializers.ValidationError({"branch": "الفرع يجب أن يتبع نفس الشركة."})
        return attrs


class JobPositionSerializer(serializers.ModelSerializer):
    class Meta:
        model = JobPosition
        fields = "__all__"

    def validate(self, attrs):
        company = attrs.get("company", getattr(self.instance, "company", None))
        department = attrs.get("department", getattr(self.instance, "department", None))
        if department and company and department.company_id != company.pk:
            raise serializers.ValidationError({"department": "الإدارة يجب أن تتبع نفس الشركة."})
        return attrs


class EmployeeSerializer(serializers.ModelSerializer):
    branch_name = serializers.CharField(source="branch.name_ar", read_only=True)
    department_name = serializers.CharField(source="department.name_ar", read_only=True)
    position_name = serializers.CharField(source="position.name_ar", read_only=True)
    currency_code = serializers.CharField(source="currency.code", read_only=True)

    class Meta:
        model = Employee
        fields = (
            "id", "company", "code", "full_name_ar", "full_name_en", "national_id",
            "insurance_number", "email", "phone", "address", "branch", "branch_name",
            "department", "department_name", "position", "position_name", "manager",
            "contract_type", "hire_date", "contract_end_date", "termination_date",
            "status", "basic_salary", "currency", "currency_code", "payment_method",
            "bank_name", "bank_account", "cost_center", "notes", "created_at", "updated_at",
        )
        read_only_fields = ("created_at", "updated_at")

    def validate(self, attrs):
        company = attrs.get("company", getattr(self.instance, "company", None))
        branch = attrs.get("branch", getattr(self.instance, "branch", None))
        if branch and company and branch.company_id != company.pk:
            raise serializers.ValidationError({"branch": "الفرع لا يتبع الشركة المحددة."})
        for field in ("department", "position", "manager", "cost_center"):
            value = attrs.get(field, getattr(self.instance, field, None))
            if value and company and value.company_id != company.pk:
                raise serializers.ValidationError({field: "القيمة المحددة لا تتبع الشركة."})
        return attrs


class PayrollComponentSerializer(serializers.ModelSerializer):
    class Meta:
        model = PayrollComponent
        fields = "__all__"

    def validate(self, attrs):
        company = attrs.get("company", getattr(self.instance, "company", None))
        for field in ("expense_account", "payable_account"):
            account = attrs.get(field, getattr(self.instance, field, None))
            if account and company and account.company_id != company.pk:
                raise serializers.ValidationError({field: "الحساب يجب أن يتبع نفس الشركة."})
        expense = attrs.get("expense_account", getattr(self.instance, "expense_account", None))
        payable = attrs.get("payable_account", getattr(self.instance, "payable_account", None))
        kind = attrs.get("kind", getattr(self.instance, "kind", None))
        method = attrs.get("method", getattr(self.instance, "method", None))
        if expense and expense.nature != "expense":
            raise serializers.ValidationError({"expense_account": "اختر حساب مصروفات."})
        if payable and payable.nature != "liability":
            raise serializers.ValidationError({"payable_account": "اختر حساب التزامات."})
        if method in (PayrollComponent.Method.EMPLOYEE_INSURANCE, PayrollComponent.Method.PROGRESSIVE_TAX) and kind != PayrollComponent.Kind.DEDUCTION:
            raise serializers.ValidationError({"kind": "التأمينات والضريبة الخاصة بالموظف يجب أن تكون استقطاعًا."})
        if method == PayrollComponent.Method.EMPLOYER_INSURANCE and kind != PayrollComponent.Kind.EMPLOYER_SHARE:
            raise serializers.ValidationError({"kind": "تأمينات صاحب العمل يجب أن تكون من نوع حصة صاحب العمل."})
        if kind == PayrollComponent.Kind.DEDUCTION and method not in (
            PayrollComponent.Method.EMPLOYEE_INSURANCE, PayrollComponent.Method.PROGRESSIVE_TAX
        ) and not payable:
            raise serializers.ValidationError({"payable_account": "اربط حساب الالتزام بهذا الاستقطاع قبل استخدامه."})
        if kind == PayrollComponent.Kind.EMPLOYER_SHARE and method != PayrollComponent.Method.EMPLOYER_INSURANCE and (not expense or not payable):
            raise serializers.ValidationError("حصة صاحب العمل المخصصة تحتاج حساب مصروف وحساب التزام.")
        return attrs


class EmployeeComponentSerializer(serializers.ModelSerializer):
    component_name = serializers.CharField(source="component.name_ar", read_only=True)

    class Meta:
        model = EmployeeComponent
        fields = "__all__"

    def validate(self, attrs):
        employee = attrs.get("employee", getattr(self.instance, "employee", None))
        component = attrs.get("component", getattr(self.instance, "component", None))
        if employee and component and employee.company_id != component.company_id:
            raise serializers.ValidationError("عنصر المرتب والموظف يجب أن يتبعا نفس الشركة.")
        return attrs


class AttendanceRecordSerializer(serializers.ModelSerializer):
    employee_code = serializers.CharField(source="employee.code", read_only=True)
    employee_name = serializers.CharField(source="employee.full_name_ar", read_only=True)

    class Meta:
        model = AttendanceRecord
        fields = "__all__"
        read_only_fields = ("entered_by",)


class LeaveRequestSerializer(serializers.ModelSerializer):
    employee_name = serializers.CharField(source="employee.full_name_ar", read_only=True)

    class Meta:
        model = LeaveRequest
        fields = "__all__"
        read_only_fields = ("approved_by", "created_at")


class EmployeeLoanSerializer(serializers.ModelSerializer):
    employee_name = serializers.CharField(source="employee.full_name_ar", read_only=True)

    class Meta:
        model = EmployeeLoan
        fields = "__all__"
        read_only_fields = ("status", "outstanding_balance")

    def validate(self, attrs):
        principal = attrs.get("principal", getattr(self.instance, "principal", 0))
        balance = attrs.get("outstanding_balance", getattr(self.instance, "outstanding_balance", principal))
        installments = attrs.get("installment_count", getattr(self.instance, "installment_count", 0))
        installment_amount = attrs.get("installment_amount", getattr(self.instance, "installment_amount", 0))
        if principal <= 0 or installments <= 0 or installment_amount <= 0 or balance < 0 or balance > principal:
            raise serializers.ValidationError("راجع مبلغ السلفة والأقساط والرصيد المتبقي.")
        employee = attrs.get("employee", getattr(self.instance, "employee", None))
        account = attrs.get("receivable_account", getattr(self.instance, "receivable_account", None))
        if account and employee and account.company_id != employee.company_id:
            raise serializers.ValidationError({"receivable_account": "حساب السلفة يجب أن يتبع نفس الشركة."})
        if not account:
            raise serializers.ValidationError({"receivable_account": "يجب تحديد حساب أصل لذمم الموظفين قبل إنشاء السلفة."})
        if account.nature != "asset" or not account.is_active or account.is_group or not account.allow_posting:
            raise serializers.ValidationError({"receivable_account": "اختر حساب أصل تفصيليًا نشطًا وقابلًا للترحيل."})
        return attrs


class PayrollRuleSetSerializer(serializers.ModelSerializer):
    class Meta:
        model = PayrollRuleSet
        fields = "__all__"

    def validate(self, attrs):
        company = attrs.get("company", getattr(self.instance, "company", None))
        country = attrs.get("country", getattr(self.instance, "country", None))
        if company and country and company.country != country:
            raise serializers.ValidationError({"country": "بلد مجموعة القواعد يجب أن يطابق بلد الشركة."})

        configuration = attrs.get("configuration", getattr(self.instance, "configuration", {}))
        if not isinstance(configuration, dict):
            raise serializers.ValidationError({"configuration": "إعدادات المرتبات يجب أن تكون كائن إعدادات صحيحًا."})

        def numeric(value, field, *, minimum=None, maximum=None):
            try:
                number = Decimal(str(value))
            except (InvalidOperation, TypeError, ValueError):
                raise serializers.ValidationError({"configuration": f"القيمة {field} يجب أن تكون رقمًا صحيحًا."})
            if not number.is_finite() or (minimum is not None and number < minimum) or (maximum is not None and number > maximum):
                raise serializers.ValidationError({"configuration": f"القيمة {field} خارج النطاق المسموح."})
            return number

        divisor = configuration.get("monthly_daily_divisor")
        if divisor is not None:
            numeric(divisor, "monthly_daily_divisor", minimum=Decimal("0.0001"))

        insurance = configuration.get("insurance", {})
        if insurance is not None:
            if not isinstance(insurance, dict):
                raise serializers.ValidationError({"configuration": "قسم insurance يجب أن يكون كائنًا."})
            min_base = numeric(insurance["min_base"], "insurance.min_base", minimum=Decimal("0")) if "min_base" in insurance else None
            max_base = numeric(insurance["max_base"], "insurance.max_base", minimum=Decimal("0")) if "max_base" in insurance else None
            if min_base is not None and max_base is not None and min_base > max_base:
                raise serializers.ValidationError({"configuration": "حد التأمين الأدنى لا يمكن أن يتجاوز الحد الأعلى."})
            for key in ("employee_rate", "employer_rate"):
                if key in insurance:
                    numeric(insurance[key], f"insurance.{key}", minimum=Decimal("0"), maximum=Decimal("100"))

        tax = configuration.get("tax", {})
        if tax is not None:
            if not isinstance(tax, dict):
                raise serializers.ValidationError({"configuration": "قسم tax يجب أن يكون كائنًا."})
            if "annual_personal_exemption" in tax:
                numeric(tax["annual_personal_exemption"], "tax.annual_personal_exemption", minimum=Decimal("0"))
            if "annual_brackets" in tax:
                brackets = tax["annual_brackets"]
                if not isinstance(brackets, list) or not brackets:
                    raise serializers.ValidationError({"configuration": "شرائح الضريبة يجب أن تكون قائمة غير فارغة."})
                previous_limit = Decimal("0")
                for index, bracket in enumerate(brackets):
                    if not isinstance(bracket, dict) or "rate" not in bracket or "up_to" not in bracket:
                        raise serializers.ValidationError({"configuration": f"شريحة الضريبة رقم {index + 1} غير مكتملة."})
                    numeric(bracket["rate"], f"tax.annual_brackets[{index}].rate", minimum=Decimal("0"), maximum=Decimal("100"))
                    upper = bracket["up_to"]
                    if upper is None:
                        if index != len(brackets) - 1:
                            raise serializers.ValidationError({"configuration": "الشريحة المفتوحة يجب أن تكون آخر شريحة."})
                    else:
                        limit = numeric(upper, f"tax.annual_brackets[{index}].up_to", minimum=Decimal("0.0001"))
                        if limit <= previous_limit:
                            raise serializers.ValidationError({"configuration": "حدود شرائح الضريبة يجب أن تكون تصاعدية."})
                        previous_limit = limit
        return attrs


class PayrollCycleSerializer(serializers.ModelSerializer):
    results_count = serializers.IntegerField(source="results.count", read_only=True)

    class Meta:
        model = PayrollCycle
        fields = (
            "id", "company", "period_start", "period_end", "payment_date", "status",
            "rule_set", "created_by", "approved_by", "approved_at", "journal_entry",
            "results_count", "created_at", "updated_at",
        )
        read_only_fields = ("status", "created_by", "approved_by", "approved_at", "journal_entry")

    def validate(self, attrs):
        start = attrs.get("period_start", getattr(self.instance, "period_start", None))
        end = attrs.get("period_end", getattr(self.instance, "period_end", None))
        payment = attrs.get("payment_date", getattr(self.instance, "payment_date", None))
        if start and end and end < start:
            raise serializers.ValidationError({"period_end": "نهاية الفترة تسبق بدايتها."})
        if start and payment and payment < start:
            raise serializers.ValidationError({"payment_date": "تاريخ الصرف لا يمكن أن يسبق بداية الفترة."})
        company = attrs.get("company", getattr(self.instance, "company", None))
        rule_set = attrs.get("rule_set", getattr(self.instance, "rule_set", None))
        if company and rule_set and rule_set.company_id != company.pk:
            raise serializers.ValidationError({"rule_set": "مجموعة القواعد يجب أن تتبع نفس الشركة."})
        if company and start and end:
            overlapping = PayrollCycle.objects.filter(company=company, period_start__lte=end, period_end__gte=start)
            if self.instance:
                overlapping = overlapping.exclude(pk=self.instance.pk)
            if overlapping.exists():
                raise serializers.ValidationError("توجد دورة مرتبات أخرى تتداخل مع هذه الفترة.")
        if self.instance and self.instance.status in ("approved", "posted", "paid"):
            raise serializers.ValidationError("الدورة المعتمدة أو المرحلة أو المصروفة غير قابلة للتعديل.")
        return attrs


class PayrollResultSerializer(serializers.ModelSerializer):
    employee_code = serializers.CharField(source="employee.code", read_only=True)
    employee_name = serializers.CharField(source="employee.full_name_ar", read_only=True)
    displayed_earnings_total = serializers.DecimalField(max_digits=18, decimal_places=4, read_only=True)
    displayed_deductions_total = serializers.DecimalField(max_digits=18, decimal_places=4, read_only=True)
    lines = serializers.SerializerMethodField()

    class Meta:
        model = PayrollResult
        fields = (
            "id", "employee", "employee_code", "employee_name", "basic_salary_snapshot",
            "earnings_total", "employee_deductions_total", "employer_share_total",
            "employer_share_display_addition", "employer_share_display_offset",
            "displayed_earnings_total", "displayed_deductions_total", "net_pay",
            "company_cost", "calculation_snapshot", "manual_adjustment_reason", "lines",
        )

    def get_lines(self, obj):
        return [{
            "code": line.code_snapshot, "name": line.name_snapshot,
            "kind": line.kind_snapshot, "amount": str(line.amount),
            "display_only": line.display_only, "calculation": line.calculation_snapshot,
        } for line in obj.lines.all()]

class EmployeeSalaryHistorySerializer(serializers.ModelSerializer):
    employee_name = serializers.CharField(source="employee.full_name_ar", read_only=True)

    class Meta:
        model = EmployeeSalaryHistory
        fields = "__all__"
        read_only_fields = ("previous_salary", "changed_by", "created_at")

    def validate(self, attrs):
        employee = attrs.get("employee", getattr(self.instance, "employee", None))
        effective_date = attrs.get("effective_date", getattr(self.instance, "effective_date", None))
        new_salary = attrs.get("new_salary", getattr(self.instance, "new_salary", None))
        if employee and effective_date and effective_date < employee.hire_date:
            raise serializers.ValidationError({"effective_date": "تاريخ سريان الراتب لا يمكن أن يسبق تاريخ التعيين."})
        if new_salary is not None and new_salary < 0:
            raise serializers.ValidationError({"new_salary": "الراتب الجديد لا يمكن أن يكون سالبًا."})
        if employee and effective_date:
            existing = EmployeeSalaryHistory.objects.filter(employee=employee, effective_date=effective_date)
            if self.instance:
                existing = existing.exclude(pk=self.instance.pk)
            if existing.exists():
                raise serializers.ValidationError({"effective_date": "يوجد تغيير راتب مسجل بالفعل في هذا التاريخ."})
        return attrs

class LoanInstallmentSerializer(serializers.ModelSerializer):
    employee_name = serializers.CharField(source="loan.employee.full_name_ar", read_only=True)
    loan_reference = serializers.CharField(source="loan.reference", read_only=True)

    class Meta:
        model = LoanInstallment
        fields = "__all__"
        read_only_fields = ("paid_amount", "paid_date", "status", "payroll_cycle")

    def validate(self, attrs):
        loan = attrs.get("loan", getattr(self.instance, "loan", None))
        amount = attrs.get("amount", getattr(self.instance, "amount", 0))
        due_date = attrs.get("due_date", getattr(self.instance, "due_date", None))
        if loan and loan.status != "active":
            raise serializers.ValidationError({"loan": "لا يمكن إضافة قسط إلى سلفة غير سارية."})
        if loan and due_date and due_date < loan.issue_date:
            raise serializers.ValidationError({"due_date": "تاريخ القسط لا يمكن أن يسبق تاريخ السلفة."})
        if amount <= 0:
            raise serializers.ValidationError({"amount": "قيمة القسط يجب أن تكون موجبة."})
        if loan:
            from django.db.models import Sum
            existing = LoanInstallment.objects.filter(loan=loan)
            if self.instance:
                existing = existing.exclude(pk=self.instance.pk)
            scheduled_total = existing.aggregate(total=Sum("amount"))["total"] or Decimal("0")
            if scheduled_total + amount > loan.principal:
                raise serializers.ValidationError({"amount": "إجمالي الأقساط يتجاوز أصل السلفة."})
        return attrs
