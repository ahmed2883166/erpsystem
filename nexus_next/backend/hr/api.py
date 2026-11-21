from django.db import transaction
from django.utils import timezone
from rest_framework import decorators, response, status, viewsets
from rest_framework.exceptions import PermissionDenied, ValidationError\nfrom rest_framework.permissions import IsAuthenticated

from accounting.models import JournalEntry
from accounting.permissions import CompanyRolePermission
from companies.access import accessible_companies, can_access_company, has_company_role
from .models import (
    AttendanceRecord, Department, Employee, EmployeeComponent, EmployeeLoan,
    JobPosition, LeaveRequest, PayrollComponent, PayrollCycle, PayrollResult,
    PayrollRuleSet,
)
from .serializers import (
    AttendanceRecordSerializer, DepartmentSerializer, EmployeeComponentSerializer,
    EmployeeLoanSerializer, EmployeeSerializer, JobPositionSerializer,
    LeaveRequestSerializer, PayrollComponentSerializer, PayrollCycleSerializer,
    PayrollResultSerializer, PayrollRuleSetSerializer,
)
from .services import calculate_payroll_cycle


class HrViewSet(viewsets.ModelViewSet):
    permission_classes = (CompanyRolePermission,)

    def get_queryset(self):
        queryset = self.queryset
        if self.request.user.is_superuser:
            return queryset
        return queryset.filter(company__in=accessible_companies(self.request.user))

    def check_company_write(self, company_id):
        if not can_access_company(self.request.user, company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الشركة المحددة.")
        if not has_company_role(self.request.user, company_id, ("admin", "accountant")):
            raise PermissionDenied("تحتاج إلى صلاحية مدير الشركة أو المحاسب.")

    def perform_create(self, serializer):
        company = serializer.validated_data["company"]
        self.check_company_write(company.pk)
        serializer.save()

    def perform_update(self, serializer):
        company = serializer.validated_data.get("company", serializer.instance.company)
        self.check_company_write(company.pk)
        serializer.save()

    def perform_destroy(self, instance):
        self.check_company_write(instance.company_id)
        if hasattr(instance, "status") and instance.status not in ("draft", "pending"):
            raise ValidationError("لا يمكن حذف سجل معتمد أو مرحّل؛ استخدم إجراء تصحيح موثق.")
        instance.delete()


class DepartmentViewSet(HrViewSet):
    queryset = Department.objects.select_related("company", "branch").all()
    serializer_class = DepartmentSerializer
    filterset_fields = ("company", "branch", "is_active")
    search_fields = ("code", "name_ar")
    ordering_fields = ("code", "name_ar")


class JobPositionViewSet(HrViewSet):
    queryset = JobPosition.objects.select_related("company", "department").all()
    serializer_class = JobPositionSerializer
    filterset_fields = ("company", "department", "is_active")
    search_fields = ("code", "name_ar")


class EmployeeViewSet(HrViewSet):
    queryset = Employee.objects.select_related("company", "branch", "department", "position", "currency").all()
    serializer_class = EmployeeSerializer
    filterset_fields = ("company", "branch", "department", "position", "status", "contract_type")
    search_fields = ("code", "full_name_ar", "full_name_en", "national_id", "insurance_number", "phone")
    ordering_fields = ("code", "full_name_ar", "hire_date", "basic_salary")

    def perform_create(self, serializer):
        self.check_company_write(serializer.validated_data["company"].pk)
        serializer.save(created_by=self.request.user)


class PayrollComponentViewSet(HrViewSet):
    queryset = PayrollComponent.objects.select_related("company", "expense_account", "payable_account").all()
    serializer_class = PayrollComponentSerializer
    filterset_fields = ("company", "kind", "method", "is_active")
    search_fields = ("code", "name_ar")


class EmployeeScopedViewSet(viewsets.ModelViewSet):\n    permission_classes = (IsAuthenticated,)\n\n    def get_queryset(self):\n        qs = self.queryset\n        if not self.request.user.is_superuser:\n            qs = qs.filter(employee__company__in=accessible_companies(self.request.user))\n        company_id = self.request.query_params.get("company")\n        if company_id:\n            qs = qs.filter(employee__company_id=company_id)\n        return qs\n\n    def _check_employee(self, employee):\n        if not can_access_company(self.request.user, employee.company_id):\n            raise PermissionDenied("لا تملك صلاحية الوصول إلى الشركة.")\n        if not has_company_role(self.request.user, employee.company_id, ("admin", "accountant")):\n            raise PermissionDenied("لا تملك صلاحية تعديل سجلات الموظف.")\n\n    def perform_create(self, serializer):\n        self._check_employee(serializer.validated_data["employee"])\n        serializer.save()\n\n    def perform_update(self, serializer):\n        employee = serializer.validated_data.get("employee", serializer.instance.employee)\n        self._check_employee(employee)\n        serializer.save()\n\n    def perform_destroy(self, instance):\n        self._check_employee(instance.employee)\n        instance.delete()\n\n\nclass EmployeeComponentViewSet(EmployeeScopedViewSet):
    queryset = EmployeeComponent.objects.select_related("employee", "component", "employee__company").all()
    serializer_class = EmployeeComponentSerializer
    permission_classes = (CompanyRolePermission,)

    def get_queryset(self):
        qs = self.queryset
        if not self.request.user.is_superuser:
            qs = qs.filter(employee__company__in=accessible_companies(self.request.user))
        company_id = self.request.query_params.get("company")
        if company_id:
            qs = qs.filter(employee__company_id=company_id)
        return qs

    def _write(self, employee):
        if not can_access_company(self.request.user, employee.company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الشركة.")
        if not has_company_role(self.request.user, employee.company_id, ("admin", "accountant")):
            raise PermissionDenied("لا تملك صلاحية تعديل عناصر مرتب الموظف.")

    def perform_create(self, serializer):
        employee = serializer.validated_data["employee"]
        self._write(employee)
        serializer.save()

    def perform_update(self, serializer):
        employee = serializer.validated_data.get("employee", serializer.instance.employee)
        self._write(employee)
        serializer.save()


class AttendanceViewSet(viewsets.ModelViewSet):
    queryset = AttendanceRecord.objects.select_related("employee", "employee__company").all()
    serializer_class = AttendanceRecordSerializer
    permission_classes = (CompanyRolePermission,)
    filterset_fields = ("employee", "employee__company", "work_date", "status")
    ordering_fields = ("work_date", "worked_hours", "overtime_hours")

    def get_queryset(self):
        qs = self.queryset
        return qs if self.request.user.is_superuser else qs.filter(employee__company__in=accessible_companies(self.request.user))

    def perform_create(self, serializer):
        employee = serializer.validated_data["employee"]
        if not has_company_role(self.request.user, employee.company_id, ("admin", "accountant")):
            raise PermissionDenied("لا تملك صلاحية إدخال الحضور.")
        serializer.save(entered_by=self.request.user)


class LeaveRequestViewSet(viewsets.ModelViewSet):
    queryset = LeaveRequest.objects.select_related("employee", "employee__company").all()
    serializer_class = LeaveRequestSerializer
    permission_classes = (CompanyRolePermission,)
    filterset_fields = ("employee", "employee__company", "kind", "status")

    def get_queryset(self):
        qs = self.queryset
        return qs if self.request.user.is_superuser else qs.filter(employee__company__in=accessible_companies(self.request.user))

    @decorators.action(detail=True, methods=("post",))
    def approve(self, request, pk=None):
        leave = self.get_object()
        if not has_company_role(request.user, leave.employee.company_id, ("admin",)):
            raise PermissionDenied("اعتماد الإجازات متاح لمدير الشركة فقط.")
        if leave.status != LeaveRequest.Status.PENDING:
            raise ValidationError("لا يمكن اعتماد أو رفض طلب تمت معالجته.")
        leave.status = LeaveRequest.Status.APPROVED
        leave.approved_by = request.user
        leave.save(update_fields=("status", "approved_by"))
        return response.Response(self.get_serializer(leave).data)


class EmployeeLoanViewSet(EmployeeScopedViewSet):
    queryset = EmployeeLoan.objects.select_related("employee", "employee__company").all()
    serializer_class = EmployeeLoanSerializer
    permission_classes = (CompanyRolePermission,)
    filterset_fields = ("employee", "employee__company", "status")

    def get_queryset(self):
        qs = self.queryset
        return qs if self.request.user.is_superuser else qs.filter(employee__company__in=accessible_companies(self.request.user))


class PayrollRuleSetViewSet(HrViewSet):
    queryset = PayrollRuleSet.objects.select_related("company").all()
    serializer_class = PayrollRuleSetSerializer
    filterset_fields = ("company", "country", "is_active")


class PayrollCycleViewSet(HrViewSet):
    queryset = PayrollCycle.objects.select_related("company", "rule_set", "journal_entry").prefetch_related("results").all()
    serializer_class = PayrollCycleSerializer
    filterset_fields = ("company", "status", "period_start", "period_end")
    ordering_fields = ("period_start", "payment_date", "status")

    def perform_create(self, serializer):
        company = serializer.validated_data["company"]
        self.check_company_write(company.pk)
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
                raise ValidationError(exc.messages)
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
        if not has_company_role(request.user, cycle.company_id, ("admin",)):
            raise PermissionDenied("اعتماد المرتبات متاح لمدير الشركة فقط.")
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
        return response.Response(PayrollResultSerializer(cycle.results.select_related("employee").prefetch_related("lines"), many=True).data)

    @decorators.action(detail=True, methods=("post",))
    def mark_paid(self, request, pk=None):
        cycle = self.get_object()
        self.check_company_write(cycle.company_id)
        if cycle.status != PayrollCycle.Status.POSTED:
            raise ValidationError("لا يمكن تسجيل الصرف قبل ترحيل قيد المرتبات.")
        raise ValidationError("تسجيل الصرف البنكي/النقدي سيتم تفعيله مع ربط الخزينة في مرحلة الترحيل المحاسبي.")
