from rest_framework import serializers

from .models import (
    AttendanceRecord, Department, Employee, EmployeeComponent, EmployeeLoan,
    JobPosition, LeaveRequest, PayrollComponent, PayrollCycle, PayrollResult,
    PayrollRuleSet,
)


class DepartmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Department
        fields = "__all__"


class JobPositionSerializer(serializers.ModelSerializer):
    class Meta:
        model = JobPosition
        fields = "__all__"


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
        if expense and expense.nature != "expense":
            raise serializers.ValidationError({"expense_account": "اختر حساب مصروفات."})
        if payable and payable.nature != "liability":
            raise serializers.ValidationError({"payable_account": "اختر حساب التزامات."})
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

    def validate(self, attrs):
        principal = attrs.get("principal", getattr(self.instance, "principal", 0))
        balance = attrs.get("outstanding_balance", getattr(self.instance, "outstanding_balance", principal))
        installments = attrs.get("installment_count", getattr(self.instance, "installment_count", 0))
        installment_amount = attrs.get("installment_amount", getattr(self.instance, "installment_amount", 0))
        if principal <= 0 or installments <= 0 or installment_amount <= 0 or balance < 0 or balance > principal:
            raise serializers.ValidationError("راجع مبلغ السلفة والأقساط والرصيد المتبقي.")
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
