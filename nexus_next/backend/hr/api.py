from django.utils import timezone
from rest_framework import decorators, response, viewsets
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated

from accounting.permissions import CompanyRolePermission
from companies.access import accessible_companies, can_access_company, has_company_role
from .models import (
    AttendanceRecord, Department, Employee, EmployeeComponent, EmployeeLoan, EmployeeSalaryHistory,
    JobPosition, LeaveRequest, PayrollComponent, PayrollCycle, PayrollResult,
    PayrollRuleSet,
)
from .serializers import (
    AttendanceRecordSerializer, DepartmentSerializer, EmployeeComponentSerializer,
    EmployeeLoanSerializer, EmployeeSerializer, JobPositionSerializer,
    LeaveRequestSerializer, EmployeeSalaryHistorySerializer, PayrollComponentSerializer, PayrollCycleSerializer,
    PayrollResultSerializer, PayrollRuleSetSerializer,
)
from .services import calculate_payroll_cycle


class CompanyScopedViewSet(viewsets.ModelViewSet):
    permission_classes = (CompanyRolePermission,)

    def get_queryset(self):
        if self.request.user.is_superuser:
            return self.queryset
        return self.queryset.filter(company__in=accessible_companies(self.request.user))

    def check_company_write(self, company_id, roles=("admin", "accountant")):
        if not can_access_company(self.request.user, company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الشركة المحددة.")
        if not has_company_role(self.request.user, company_id, roles):
            raise PermissionDenied("تحتاج إلى صلاحية مدير الشركة أو المحاسب.")

    def perform_create(self, serializer):
        self.check_company_write(serializer.validated_data["company"].pk)
        serializer.save()

    def perform_update(self, serializer):
        company = serializer.validated_data.get("company", serializer.instance.company)
        self.check_company_write(company.pk)
        serializer.save()

    def perform_destroy(self, instance):
        self.check_company_write(instance.company_id, roles=("admin",))
        if hasattr(instance, "status") and instance.status not in ("draft", "pending"):
            raise ValidationError("لا يمكن حذف سجل معتمد أو مرحّل؛ استخدم إجراء تصحيح موثق.")
        instance.delete()


class EmployeeScopedViewSet(viewsets.ModelViewSet):
    permission_classes = (IsAuthenticated,)

    def get_queryset(self):
        qs = self.queryset
        if not self.request.user.is_superuser:
            qs = qs.filter(employee__company__in=accessible_companies(self.request.user))
        company_id = self.request.query_params.get("company")
        if company_id:
            qs = qs.filter(employee__company_id=company_id)
        return qs

    def check_employee_write(self, employee, admin_only=False):
        roles = ("admin",) if admin_only else ("admin", "accountant")
        if not can_access_company(self.request.user, employee.company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الشركة.")
        if not has_company_role(self.request.user, employee.company_id, roles):
            raise PermissionDenied("لا تملك صلاحية تعديل سجلات الموظف.")

    def perform_create(self, serializer):
        self.check_employee_write(serializer.validated_data["employee"])
        serializer.save()

    def perform_update(self, serializer):
        employee = serializer.validated_data.get("employee", serializer.instance.employee)
        self.check_employee_write(employee)
        serializer.save()

    def perform_destroy(self, instance):
        self.check_employee_write(instance.employee, admin_only=True)
        instance.delete()


class DepartmentViewSet(CompanyScopedViewSet):
    queryset = Department.objects.select_related("company", "branch").all()
    serializer_class = DepartmentSerializer
    filterset_fields = ("company", "branch", "is_active")
    search_fields = ("code", "name_ar")
    ordering_fields = ("code", "name_ar")


class JobPositionViewSet(CompanyScopedViewSet):
    queryset = JobPosition.objects.select_related("company", "department").all()
    serializer_class = JobPositionSerializer
    filterset_fields = ("company", "department", "is_active")
    search_fields = ("code", "name_ar")


class EmployeeViewSet(CompanyScopedViewSet):
    queryset = Employee.objects.select_related("company", "branch", "department", "position", "currency").all()
    serializer_class = EmployeeSerializer
    filterset_fields = ("company", "branch", "department", "position", "status", "contract_type")
    search_fields = ("code", "full_name_ar", "full_name_en", "national_id", "insurance_number", "phone")
    ordering_fields = ("code", "full_name_ar", "hire_date", "basic_salary")

    def perform_create(self, serializer):
        self.check_company_write(serializer.validated_data["company"].pk)
        serializer.save(created_by=self.request.user)


class PayrollComponentViewSet(CompanyScopedViewSet):
    queryset = PayrollComponent.objects.select_related("company", "expense_account", "payable_account").all()
    serializer_class = PayrollComponentSerializer
    filterset_fields = ("company", "kind", "method", "is_active")
    search_fields = ("code", "name_ar")


class EmployeeComponentViewSet(EmployeeScopedViewSet):
    queryset = EmployeeComponent.objects.select_related("employee", "component", "employee__company").all()
    serializer_class = EmployeeComponentSerializer


class AttendanceViewSet(EmployeeScopedViewSet):
    queryset = AttendanceRecord.objects.select_related("employee", "employee__company").all()
    serializer_class = AttendanceRecordSerializer
    filterset_fields = ("employee", "employee__company", "work_date", "status")
    ordering_fields = ("work_date", "worked_hours", "overtime_hours")

    def perform_create(self, serializer):
        self.check_employee_write(serializer.validated_data["employee"])
        serializer.save(entered_by=self.request.user)


class LeaveRequestViewSet(EmployeeScopedViewSet):
    queryset = LeaveRequest.objects.select_related("employee", "employee__company").all()
    serializer_class = LeaveRequestSerializer
    filterset_fields = ("employee", "employee__company", "kind", "status")

    @decorators.action(detail=True, methods=("post",))
    def approve(self, request, pk=None):
        leave = self.get_object()
        self.check_employee_write(leave.employee, admin_only=True)
        if leave.status != LeaveRequest.Status.PENDING:
            raise ValidationError("لا يمكن اعتماد أو رفض طلب تمت معالجته.")
        leave.status = LeaveRequest.Status.APPROVED
        leave.approved_by = request.user
        leave.save(update_fields=("status", "approved_by"))
        return response.Response(self.get_serializer(leave).data)


class EmployeeLoanViewSet(EmployeeScopedViewSet):
    queryset = EmployeeLoan.objects.select_related("employee", "employee__company").all()
    serializer_class = EmployeeLoanSerializer
    filterset_fields = ("employee", "employee__company", "status")


class EmployeeSalaryHistoryViewSet(EmployeeScopedViewSet):
    queryset = EmployeeSalaryHistory.objects.select_related("employee", "employee__company", "changed_by").all()
    serializer_class = EmployeeSalaryHistorySerializer
    filterset_fields = ("employee", "employee__company", "change_type", "effective_date")
    http_method_names = ("get", "post", "head", "options")

    def perform_create(self, serializer):
        from datetime import timedelta
        from django.db import transaction
        from .services import salary_for_date
        employee = serializer.validated_data["employee"]
        self.check_employee_write(employee)
        effective_date = serializer.validated_data["effective_date"]
        with transaction.atomic():
            locked = Employee.objects.select_for_update().get(pk=employee.pk)
            previous = salary_for_date(locked, effective_date - timedelta(days=1))
            history = serializer.save(previous_salary=previous, changed_by=self.request.user)
            if effective_date <= timezone.localdate():
                locked.basic_salary = history.new_salary
                locked.save(update_fields=("basic_salary", "updated_at"))


class PayrollRuleSetViewSet(CompanyScopedViewSet):
    queryset = PayrollRuleSet.objects.select_related("company").all()
    serializer_class = PayrollRuleSetSerializer
    filterset_fields = ("company", "country", "is_active")


class PayrollCycleViewSet(CompanyScopedViewSet):
    queryset = PayrollCycle.objects.select_related("company", "rule_set", "journal_entry").prefetch_related("results").all()
    serializer_class = PayrollCycleSerializer
    filterset_fields = ("company", "status", "period_start", "period_end")
    ordering_fields = ("period_start", "payment_date", "status")

    def perform_create(self, serializer):
        self.check_company_write(serializer.validated_data["company"].pk)
        serializer.save(created_by=self.request.user)

    @decorators.action(detail=True, methods=("post",))
    def calculate(self, request, pk=None):
        cycle = self.get_object()
        self.check_company_write(cycle.company_id)
        try:
            result = calculate_payroll_cycle(cycle.pk, request.user)
        except Exception as exc:
            from django.core.exceptions import ValidationError as DjangoValidationError
            if isinstance(exc, DjangoValidationError):
                raise ValidationError(exc.messages) from exc
            raise
        return response.Response(result)

    @decorators.action(detail=True, methods=("post",))
    def send_to_review(self, request, pk=None):
        cycle = self.get_object()
        self.check_company_write(cycle.company_id)
        if cycle.status != PayrollCycle.Status.CALCULATED:
            raise ValidationError("يجب احتساب الدورة أولًا قبل إرسالها للمراجعة.")
        cycle.status = PayrollCycle.Status.REVIEW
        cycle.save(update_fields=("status", "updated_at"))
        return response.Response(self.get_serializer(cycle).data)

    @decorators.action(detail=True, methods=("post",))
    def approve(self, request, pk=None):
        cycle = self.get_object()
        self.check_company_write(cycle.company_id, roles=("admin",))
        if cycle.status != PayrollCycle.Status.REVIEW:
            raise ValidationError("الدورة يجب أن تكون تحت المراجعة قبل الاعتماد.")
        cycle.status = PayrollCycle.Status.APPROVED
        cycle.approved_by = request.user
        cycle.approved_at = timezone.now()
        cycle.save(update_fields=("status", "approved_by", "approved_at", "updated_at"))
        return response.Response(self.get_serializer(cycle).data)

    @decorators.action(detail=True, methods=("get",))
    def results(self, request, pk=None):
        cycle = self.get_object()
        rows = cycle.results.select_related("employee").prefetch_related("lines")
        return response.Response(PayrollResultSerializer(rows, many=True).data)
