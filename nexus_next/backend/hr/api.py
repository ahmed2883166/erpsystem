from decimal import Decimal

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import decorators, response, status, viewsets
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated

from accounting.permissions import CompanyRolePermission
from audit.models import AuditEvent
from companies.models import FiscalYear
from treasury.models import TreasuryAccount, TreasuryTransaction
from treasury.services import post_treasury_transaction
from approvals.models import FinancialApprovalRequest
from approvals.services import refresh_financial_approval

from companies.access import accessible_branches, accessible_companies, can_access_branch, can_access_company, has_company_role, is_platform_owner
from .models import (
    AttendanceRecord, Department, Employee, EmployeeComponent, EmployeeLoan, EmployeeSalaryHistory, LoanInstallment,
    JobPosition, LeaveRequest, PayrollComponent, PayrollCycle, PayrollResult,
    PayrollRuleSet,
)
from .serializers import (
    AttendanceRecordSerializer, DepartmentSerializer, EmployeeComponentSerializer,
    EmployeeLoanSerializer, EmployeeSerializer, JobPositionSerializer, LoanInstallmentSerializer,
    LeaveRequestSerializer, EmployeeSalaryHistorySerializer, PayrollComponentSerializer, PayrollCycleSerializer,
    PayrollResultSerializer, PayrollRuleSetSerializer,
)
from .services import calculate_payroll_cycle, post_payroll_cycle, prepare_payroll_payment, complete_payroll_payment, cancel_payroll_payment_preparation


class CompanyScopedViewSet(viewsets.ModelViewSet):
    permission_classes = (CompanyRolePermission,)

    def get_queryset(self):
        """تجهيز السجلات المسموح بعرضها بعد تطبيق نطاق الشركة والفرع."""
        if self.request.user.is_superuser and not is_platform_owner(self.request.user):
            return self.queryset
        return self.queryset.filter(company__in=accessible_companies(self.request.user))

    def check_company_write(self, company_id, roles=("admin", "accountant")):
        """التحقق من دور المستخدم قبل تعديل بيانات الشركة أو المرتبات."""
        if not can_access_company(self.request.user, company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الشركة المحددة.")
        if not has_company_role(self.request.user, company_id, roles):
            raise PermissionDenied("تحتاج إلى صلاحية مدير الشركة أو المحاسب.")

    def perform_create(self, serializer):
        """حفظ سجل جديد بعد التحقق من الصلاحيات والعلاقات المرتبطة."""
        self.check_company_write(serializer.validated_data["company"].pk)
        serializer.save()

    def perform_update(self, serializer):
        """حفظ التعديلات المسموح بها بعد إعادة فحص نطاق السجل."""
        company = serializer.validated_data.get("company", serializer.instance.company)
        self.check_company_write(company.pk)
        serializer.save()

    def perform_destroy(self, instance):
        """حذف أو تعطيل السجل وفق قيود الارتباط والحالة المالية."""
        self.check_company_write(instance.company_id, roles=("admin",))
        if hasattr(instance, "status") and instance.status not in ("draft", "pending"):
            raise ValidationError("لا يمكن حذف سجل معتمد أو مرحّل؛ استخدم إجراء تصحيح موثق.")
        instance.delete()


class EmployeeScopedViewSet(viewsets.ModelViewSet):
    permission_classes = (IsAuthenticated,)

    def get_queryset(self):
        """تجهيز السجلات المسموح بعرضها بعد تطبيق نطاق الشركة والفرع."""
        qs = self.queryset
        if not self.request.user.is_superuser or is_platform_owner(self.request.user):
            qs = qs.filter(
                employee__company__in=accessible_companies(self.request.user),
                employee__branch__in=accessible_branches(self.request.user),
            )
        company_id = self.request.query_params.get("company")
        if company_id:
            qs = qs.filter(employee__company_id=company_id)
        return qs

    def check_employee_write(self, employee, admin_only=False):
        """التحقق من صلاحية المستخدم لتعديل ملف الموظف أو مستنداته."""
        roles = ("admin",) if admin_only else ("admin", "accountant")
        if not can_access_company(self.request.user, employee.company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الشركة.")
        if not has_company_role(self.request.user, employee.company_id, roles):
            raise PermissionDenied("لا تملك صلاحية تعديل سجلات الموظف.")
        if not can_access_branch(self.request.user, employee.branch_id, employee.company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع الموظف.")

    def perform_create(self, serializer):
        """حفظ سجل جديد بعد التحقق من الصلاحيات والعلاقات المرتبطة."""
        self.check_employee_write(serializer.validated_data["employee"])
        serializer.save(created_by=self.request.user)

    def perform_update(self, serializer):
        """حفظ التعديلات المسموح بها بعد إعادة فحص نطاق السجل."""
        employee = serializer.validated_data.get("employee", serializer.instance.employee)
        self.check_employee_write(employee)
        serializer.save()

    def perform_destroy(self, instance):
        """حذف أو تعطيل السجل وفق قيود الارتباط والحالة المالية."""
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

    def get_queryset(self):
        """تجهيز السجلات المسموح بعرضها بعد تطبيق نطاق الشركة والفرع."""
        qs = super().get_queryset()
        if not self.request.user.is_superuser or is_platform_owner(self.request.user):
            qs = qs.filter(branch__in=accessible_branches(self.request.user))
        return qs

    def perform_create(self, serializer):
        """حفظ سجل جديد بعد التحقق من الصلاحيات والعلاقات المرتبطة."""
        company = serializer.validated_data["company"]
        branch = serializer.validated_data["branch"]
        self.check_company_write(company.pk)
        if not can_access_branch(self.request.user, branch.pk, company.pk):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع الموظف.")
        serializer.save(created_by=self.request.user)

    def perform_update(self, serializer):
        """حفظ التعديلات المسموح بها بعد إعادة فحص نطاق السجل."""
        company = serializer.validated_data.get("company", serializer.instance.company)
        branch = serializer.validated_data.get("branch", serializer.instance.branch)
        self.check_company_write(company.pk)
        if not can_access_branch(self.request.user, branch.pk, company.pk):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع الموظف.")
        serializer.save()

    def perform_destroy(self, instance):
        """حذف أو تعطيل السجل وفق قيود الارتباط والحالة المالية."""
        self.check_company_write(instance.company_id, roles=("admin",))
        related = (
            ("عناصر الأجر", instance.compensation_items.exists()),
            ("سجلات الحضور", instance.attendance.exists()),
            ("طلبات الإجازة", instance.leave_requests.exists()),
            ("السلف", instance.loans.exists()),
            ("تاريخ الرواتب", instance.salary_history.exists()),
            ("نتائج المرتبات", instance.payroll_results.exists()),
        )
        linked = [label for label, exists in related if exists]
        if linked:
            raise ValidationError({"employee": "لا يمكن حذف الموظف لوجود سجلات مرتبطة: " + "، ".join(linked) + ". أوقف الموظف بدلًا من حذفه."})
        instance.delete()


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
        """حفظ سجل جديد بعد التحقق من الصلاحيات والعلاقات المرتبطة."""
        self.check_employee_write(serializer.validated_data["employee"])
        serializer.save(entered_by=self.request.user)


class LeaveRequestViewSet(EmployeeScopedViewSet):
    queryset = LeaveRequest.objects.select_related("employee", "employee__company").all()
    serializer_class = LeaveRequestSerializer
    filterset_fields = ("employee", "employee__company", "kind", "status")

    def perform_create(self, serializer):
        employee = serializer.validated_data["employee"]
        self.check_employee_write(employee)
        serializer.save(created_by=self.request.user)

    @decorators.action(detail=True, methods=("post",))
    @transaction.atomic
    def approve(self, request, pk=None):
        """اعتماد طلب الإجازة مع منع الاعتماد الذاتي وتوثيق الاستثناء الأعلى."""
        leave = LeaveRequest.objects.select_for_update().select_related("employee", "employee__company").get(pk=self.get_object().pk)
        self.check_employee_write(leave.employee, admin_only=True)
        if leave.status != LeaveRequest.Status.PENDING:
            raise ValidationError("لا يمكن اعتماد أو رفض طلب تمت معالجته.")
        if leave.created_by_id == request.user.pk or leave.employee.user_id == request.user.pk:
            reason = str(request.data.get("self_approval_override_reason", "")).strip()
            if not request.user.is_superuser or len(reason) < 10:
                raise PermissionDenied("لا يجوز اعتماد طلب الإجازة الذي أنشأته أو الخاص بحسابك الوظيفي. الاستثناء محصور بمشرف النظام مع سبب مكتوب لا يقل عن 10 أحرف.")
            AuditEvent.objects.create(actor=request.user, company=leave.employee.company, branch=leave.employee.branch, action=AuditEvent.Action.ACTION, resource="hr.leave.self_approval_override", object_id=str(leave.pk), http_method="POST", status_code=200, succeeded=True, metadata={"reason": reason, "employee_id": leave.employee_id, "employee_login_match": leave.employee.user_id == request.user.pk, "decision": "approved"})
        leave.status = LeaveRequest.Status.APPROVED
        leave.approved_by = request.user
        leave.save(update_fields=("status", "approved_by"))
        return response.Response(self.get_serializer(leave).data)

    @decorators.action(detail=True, methods=("post",))
    @transaction.atomic
    def reject(self, request, pk=None):
        """رفض طلب الإجازة مع منع تعارض المصالح وتوثيق الاستثناء الأعلى."""
        leave = LeaveRequest.objects.select_for_update().select_related("employee", "employee__company").get(pk=self.get_object().pk)
        self.check_employee_write(leave.employee, admin_only=True)
        if leave.status != LeaveRequest.Status.PENDING:
            raise ValidationError("لا يمكن رفض طلب تمت معالجته.")
        if leave.created_by_id == request.user.pk or leave.employee.user_id == request.user.pk:
            reason = str(request.data.get("self_approval_override_reason", "")).strip()
            if not request.user.is_superuser or len(reason) < 10:
                raise PermissionDenied("لا يجوز اتخاذ قرار في طلب الإجازة الذي أنشأته أو الخاص بحسابك الوظيفي. الاستثناء محصور بمشرف النظام مع سبب مكتوب لا يقل عن 10 أحرف.")
            AuditEvent.objects.create(actor=request.user, company=leave.employee.company, branch=leave.employee.branch, action=AuditEvent.Action.ACTION, resource="hr.leave.self_approval_override", object_id=str(leave.pk), http_method="POST", status_code=200, succeeded=True, metadata={"reason": reason, "employee_id": leave.employee_id, "employee_login_match": leave.employee.user_id == request.user.pk, "decision": "rejected"})
        leave.status = LeaveRequest.Status.REJECTED
        leave.approved_by = request.user
        leave.save(update_fields=("status", "approved_by"))
        return response.Response(self.get_serializer(leave).data)


class EmployeeLoanViewSet(EmployeeScopedViewSet):
    queryset = EmployeeLoan.objects.select_related("employee", "employee__company").all()
    serializer_class = EmployeeLoanSerializer
    filterset_fields = ("employee", "employee__company", "status")
    http_method_names = ("get", "post", "head", "options")

    def perform_create(self, serializer):
        """حفظ سجل جديد بعد التحقق من الصلاحيات والعلاقات المرتبطة."""
        employee = serializer.validated_data["employee"]
        self.check_employee_write(employee)
        serializer.save(outstanding_balance=serializer.validated_data["principal"], status=EmployeeLoan.Status.DRAFT)

    @decorators.action(detail=False, methods=("post",), url_path="create-with-schedule")
    def create_with_schedule(self, request):
        """تنفيذ وظيفة create_with_schedule ضمن واجهات api.py مع احترام دورة المستند والصلاحيات المحاسبية."""
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        employee = serializer.validated_data["employee"]
        self.check_employee_write(employee)
        schedule = request.data.get("installments")
        if not isinstance(schedule, list) or len(schedule) != serializer.validated_data["installment_count"]:
            raise ValidationError({"installments": "عدد الأقساط في الجدول يجب أن يطابق عدد الأقساط في السلفة."})
        amounts = []
        normalized = []
        from django.utils.dateparse import parse_date
        previous_due_date = None
        for row in schedule:
            due_date = parse_date(str(row.get("due_date", "")))
            amount = Decimal(str(row.get("amount", "0")))
            if not due_date or amount <= 0:
                raise ValidationError({"installments": "كل قسط يحتاج تاريخ استحقاق وقيمة موجبة."})
            if due_date < serializer.validated_data["issue_date"] or (previous_due_date and due_date <= previous_due_date):
                raise ValidationError({"installments": "تواريخ الأقساط يجب أن تكون بعد تاريخ السلفة ومرتبة تصاعديًا."})
            amounts.append(amount)
            normalized.append((due_date, amount))
            previous_due_date = due_date
        principal = serializer.validated_data["principal"]
        if abs(sum(amounts, Decimal("0")) - principal) > Decimal("0.0001"):
            raise ValidationError({"installments": "مجموع الأقساط يجب أن يساوي أصل السلفة."})
        with transaction.atomic():
            loan = serializer.save(outstanding_balance=principal, status=EmployeeLoan.Status.DRAFT)
            LoanInstallment.objects.bulk_create([
                LoanInstallment(loan=loan, due_date=due_date, amount=amount)
                for due_date, amount in normalized
            ])
        return response.Response(self.get_serializer(loan).data, status=status.HTTP_201_CREATED)

    @decorators.action(detail=True, methods=("post",), url_path="disburse")
    @transaction.atomic
    def disburse(self, request, pk=None):
        """إعداد صرف السلفة من الخزينة وترحيله بعد استيفاء الاعتماد المالي."""
        loan = EmployeeLoan.objects.select_for_update().select_related(
            "employee__company__base_currency", "employee__branch", "receivable_account",
            "disbursement_transaction",
        ).get(pk=self.get_object().pk)
        employee = loan.employee
        self.check_employee_write(employee)

        if loan.status in (EmployeeLoan.Status.SETTLED, EmployeeLoan.Status.SUSPENDED):
            raise ValidationError("لا يمكن صرف سلفة مسددة أو موقوفة.")
        if loan.disbursement_transaction_id:
            treasury_doc = TreasuryTransaction.objects.select_for_update().select_related(
                "source_account", "counter_account", "fiscal_year",
            ).get(pk=loan.disbursement_transaction_id)
            if treasury_doc.status == TreasuryTransaction.Status.POSTED:
                if loan.status != EmployeeLoan.Status.ACTIVE:
                    loan.status = EmployeeLoan.Status.ACTIVE
                    loan.disbursed_at = loan.disbursed_at or timezone.now()
                    loan.disbursed_by = loan.disbursed_by or request.user
                    loan.save(update_fields=("status", "disbursed_at", "disbursed_by"))
                return response.Response(self.get_serializer(loan).data)
            if treasury_doc.status != TreasuryTransaction.Status.DRAFT:
                raise ValidationError("مستند صرف السلفة ملغي؛ راجع السلفة وأنشئ إجراء تصحيح معتمد.")
        else:
            if loan.status != EmployeeLoan.Status.DRAFT:
                raise ValidationError("السلفة الحالية لا تحتوي على مستند صرف؛ لا يمكن إنشاء صرف مكرر تلقائيًا.")
            if not loan.receivable_account_id:
                raise ValidationError({"receivable_account": "حدد حساب ذمم سلف الموظفين قبل الصرف."})
            if (
                loan.receivable_account.company_id != employee.company_id
                or loan.receivable_account.nature != "asset"
                or loan.receivable_account.is_group
                or not loan.receivable_account.allow_posting
                or not loan.receivable_account.is_active
            ):
                raise ValidationError({"receivable_account": "حساب ذمم السلف يجب أن يكون أصلًا تفصيليًا نشطًا قابلًا للترحيل."})
            treasury_account_id = request.data.get("treasury_account")
            if not treasury_account_id:
                raise ValidationError({"treasury_account": "اختر الخزينة أو البنك الذي سيتم صرف السلفة منه."})
            try:
                treasury_account = TreasuryAccount.objects.select_for_update().select_related(
                    "account", "company__base_currency",
                ).get(pk=treasury_account_id, company_id=employee.company_id)
            except (TreasuryAccount.DoesNotExist, TypeError, ValueError):
                raise ValidationError({"treasury_account": "حساب الخزينة غير موجود ضمن الشركة."})
            if not treasury_account.is_active or treasury_account.account.nature != "asset":
                raise ValidationError({"treasury_account": "اختر حساب خزينة نشطًا مرتبطًا بحساب أصول."})
            if treasury_account.branch_id and treasury_account.branch_id != employee.branch_id:
                raise ValidationError({"treasury_account": "فرع الخزينة لا يطابق فرع الموظف."})
            if treasury_account.currency_code != employee.company.base_currency.code or employee.currency_id != employee.company.base_currency_id:
                raise ValidationError({"treasury_account": "صرف السلف متاح حاليًا بالعملة الأساسية للشركة فقط؛ لا تستخدم حساب خزينة أجنبي."})
            if treasury_account.account_id == loan.receivable_account_id:
                raise ValidationError({"receivable_account": "حساب ذمم السلف يجب أن يختلف عن حساب الخزينة."})
            fiscal_year = FiscalYear.objects.filter(
                company_id=employee.company_id, status=FiscalYear.Status.OPEN,
                start_date__lte=loan.issue_date, end_date__gte=loan.issue_date,
            ).first()
            if not fiscal_year:
                raise ValidationError("لا توجد سنة مالية مفتوحة تغطي تاريخ السلفة.")
            treasury_doc = TreasuryTransaction.objects.create(
                company=employee.company, branch=employee.branch, fiscal_year=fiscal_year,
                source_account=treasury_account, counter_account=loan.receivable_account,
                kind=TreasuryTransaction.Kind.PAYMENT,
                document_number=f"LOAN-{loan.pk}",
                transaction_date=loan.issue_date, amount=loan.principal,
                description=f"صرف سلفة الموظف {employee.full_name_ar} رقم {loan.reference}"[:500],
                cash_flow_category=TreasuryTransaction.CashFlowCategory.FINANCING,
                created_by=request.user,
            )
            loan.disbursement_transaction = treasury_doc
            loan.status = EmployeeLoan.Status.PENDING
            loan.save(update_fields=("disbursement_transaction", "status"))
            refresh_financial_approval(
                FinancialApprovalRequest.DocumentType.TREASURY_PAYMENT,
                treasury_doc, request.user,
            )

        try:
            posted = post_treasury_transaction(treasury_doc.pk, request.user)
        except DjangoValidationError as exc:
            detail = exc.message_dict if hasattr(exc, "message_dict") else exc.messages
            messages = detail.values() if isinstance(detail, dict) else detail
            flattened = " ".join(str(item) for value in messages for item in (value if isinstance(value, (list, tuple)) else [value]))
            if "تجاوز المستند حد الاعتماد المالي" in flattened:
                return response.Response({
                    **self.get_serializer(loan).data,
                    "approval_pending": True,
                    "detail": "تم تجهيز مستند صرف السلفة؛ يلزم اعتماد حد الصرف المالي قبل إعادة تنفيذ الصرف.",
                }, status=status.HTTP_202_ACCEPTED)
            raise ValidationError(detail) from exc

        loan.status = EmployeeLoan.Status.ACTIVE
        loan.disbursed_at = timezone.now()
        loan.disbursed_by = request.user
        loan.save(update_fields=("status", "disbursed_at", "disbursed_by"))
        return response.Response(self.get_serializer(loan).data, status=status.HTTP_200_OK)


class EmployeeSalaryHistoryViewSet(EmployeeScopedViewSet):
    queryset = EmployeeSalaryHistory.objects.select_related("employee", "employee__company", "changed_by").all()
    serializer_class = EmployeeSalaryHistorySerializer
    filterset_fields = ("employee", "employee__company", "change_type", "effective_date")
    http_method_names = ("get", "post", "head", "options")

    def perform_create(self, serializer):
        """حفظ سجل جديد بعد التحقق من الصلاحيات والعلاقات المرتبطة."""
        from datetime import timedelta
        from django.db import transaction
        from .services import salary_for_date
        employee = serializer.validated_data["employee"]
        self.check_employee_write(employee)
        effective_date = serializer.validated_data["effective_date"]
        with transaction.atomic():
            locked = Employee.objects.select_for_update().get(pk=employee.pk)
            if EmployeeSalaryHistory.objects.filter(employee=locked, effective_date=effective_date).exists():
                raise ValidationError({"effective_date": "يوجد تغيير راتب مسجل بالفعل في هذا التاريخ."})
            previous = salary_for_date(locked, effective_date - timedelta(days=1))
            serializer.save(previous_salary=previous, changed_by=self.request.user)
            if effective_date <= timezone.localdate():
                locked.basic_salary = salary_for_date(locked, timezone.localdate())
                locked._salary_history_update = True
                try:
                    locked.save(update_fields=("basic_salary", "updated_at"))
                finally:
                    del locked._salary_history_update


class PayrollRuleSetViewSet(CompanyScopedViewSet):
    queryset = PayrollRuleSet.objects.select_related("company").all()
    serializer_class = PayrollRuleSetSerializer
    filterset_fields = ("company", "country", "is_active")


class PayrollCycleViewSet(CompanyScopedViewSet):
    queryset = PayrollCycle.objects.select_related("company", "rule_set", "journal_entry").prefetch_related("results").all()
    serializer_class = PayrollCycleSerializer
    filterset_fields = ("company", "status", "period_start", "period_end")
    ordering_fields = ("period_start", "payment_date", "status")

    def get_queryset(self):
        """تجهيز السجلات المسموح بعرضها بعد تطبيق نطاق الشركة والفرع."""
        qs = super().get_queryset()
        if not self.request.user.is_superuser or is_platform_owner(self.request.user):
            from companies.models import CompanyMembership
            company_ids = CompanyMembership.objects.filter(
                user=self.request.user, is_active=True, all_branches=True,
                role__in=("admin", "accountant"),
            ).values("company_id")
            qs = qs.filter(company_id__in=company_ids)
        return qs

    def check_company_write(self, company_id, roles=("admin", "accountant")):
        """التحقق من دور المستخدم قبل تعديل بيانات الشركة أو المرتبات."""
        super().check_company_write(company_id, roles=roles)
        if not self.request.user.is_superuser or is_platform_owner(self.request.user):
            from companies.models import CompanyMembership
            allowed = CompanyMembership.objects.filter(
                user=self.request.user, company_id=company_id, is_active=True,
                all_branches=True, role__in=roles,
            ).exists()
            if not allowed:
                raise PermissionDenied("إدارة دورة المرتبات تتطلب صلاحية على جميع فروع الشركة.")

    def perform_create(self, serializer):
        """حفظ سجل جديد بعد التحقق من الصلاحيات والعلاقات المرتبطة."""
        self.check_company_write(serializer.validated_data["company"].pk)
        serializer.save(created_by=self.request.user)

    @decorators.action(detail=True, methods=("post",))
    def calculate(self, request, pk=None):
        """تشغيل احتساب دورة المرتبات وفق الإعدادات المعتمدة."""
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
        """إرسال دورة المرتبات للمراجعة قبل اعتمادها."""
        cycle = self.get_object()
        self.check_company_write(cycle.company_id)
        if cycle.status != PayrollCycle.Status.CALCULATED:
            raise ValidationError("يجب احتساب الدورة أولًا قبل إرسالها للمراجعة.")
        cycle.status = PayrollCycle.Status.REVIEW
        cycle.save(update_fields=("status", "updated_at"))
        return response.Response(self.get_serializer(cycle).data)

    @decorators.action(detail=True, methods=("post",))
    @transaction.atomic
    def approve(self, request, pk=None):
        """اعتماد دورة المرتبات مع قفل السجل ومنع الاعتماد الذاتي."""
        cycle = self.get_object()
        self.check_company_write(cycle.company_id, roles=("admin",))
        cycle = PayrollCycle.objects.select_for_update().get(pk=cycle.pk)
        if cycle.status != PayrollCycle.Status.REVIEW:
            raise ValidationError("الدورة يجب أن تكون تحت المراجعة قبل الاعتماد.")
        if cycle.created_by_id == request.user.pk:
            reason = str(request.data.get("self_approval_override_reason", "")).strip()
            if not request.user.is_superuser or len(reason) < 10:
                raise PermissionDenied("لا يجوز اعتماد دورة المرتبات التي أنشأتها. الاستثناء محصور بمشرف النظام مع سبب مكتوب لا يقل عن 10 أحرف.")
            AuditEvent.objects.create(actor=request.user, company=cycle.company, action=AuditEvent.Action.ACTION, resource="hr.payroll.self_approval_override", object_id=str(cycle.pk), http_method="POST", status_code=200, succeeded=True, metadata={"reason": reason, "decision": "approved"})
        cycle.status = PayrollCycle.Status.APPROVED
        cycle.approved_by = request.user
        cycle.approved_at = timezone.now()
        cycle.save(update_fields=("status", "approved_by", "approved_at", "updated_at"))
        return response.Response(self.get_serializer(cycle).data)

    @decorators.action(detail=True, methods=("get",))
    def results(self, request, pk=None):
        """عرض نتائج احتساب المرتبات وتفاصيل الموظفين."""
        cycle = self.get_object()
        rows = cycle.results.select_related("employee").prefetch_related("lines")
        return response.Response(PayrollResultSerializer(rows, many=True).data)

    @decorators.action(detail=True, methods=("post",))
    def post(self, request, pk=None):
        """ترحيل المستند أو تنفيذ إجراء الترحيل المرتبط به."""
        cycle = self.get_object()
        self.check_company_write(cycle.company_id, roles=("admin", "accountant"))
        try:
            result = post_payroll_cycle(cycle.pk, request.user)
        except Exception as exc:
            from django.core.exceptions import ValidationError as DjangoValidationError
            if isinstance(exc, DjangoValidationError):
                raise ValidationError(exc.messages) from exc
            raise
        return response.Response(result)

    @decorators.action(detail=True, methods=("post",))
    def prepare_payment(self, request, pk=None):
        """إعداد عملية صرف المرتبات وربط حساب الخزينة."""
        cycle = self.get_object()
        self.check_company_write(cycle.company_id, roles=("admin", "accountant"))
        treasury_account_id = request.data.get("treasury_account_id")
        if not treasury_account_id:
            raise ValidationError({"treasury_account_id": "اختر حساب الخزينة أو البنك للصرف."})
        try:
            result = prepare_payroll_payment(cycle.pk, treasury_account_id, request.user)
        except Exception as exc:
            from django.core.exceptions import ValidationError as DjangoValidationError
            if isinstance(exc, DjangoValidationError):
                raise ValidationError(exc.messages) from exc
            raise
        return response.Response(result)

    @decorators.action(detail=True, methods=("post",))
    def complete_payment(self, request, pk=None):
        """إتمام صرف المرتبات وتحديث حالة السداد."""
        cycle = self.get_object()
        self.check_company_write(cycle.company_id, roles=("admin", "accountant"))
        try:
            result = complete_payroll_payment(cycle.pk, request.user)
        except Exception as exc:
            from django.core.exceptions import ValidationError as DjangoValidationError
            if isinstance(exc, DjangoValidationError):
                raise ValidationError(exc.messages) from exc
            raise
        return response.Response(result)


    @decorators.action(detail=True, methods=("post",))
    def cancel_payment_preparation(self, request, pk=None):
        """إلغاء إعداد الصرف المسودة وإعادة الدورة إلى حالة مستحقات مرحلة."""
        cycle = self.get_object()
        self.check_company_write(cycle.company_id, roles=("admin", "accountant"))
        try:
            result = cancel_payroll_payment_preparation(cycle.pk, request.user)
        except Exception as exc:
            from django.core.exceptions import ValidationError as DjangoValidationError
            if isinstance(exc, DjangoValidationError):
                raise ValidationError(exc.messages) from exc
            raise
        return response.Response(result)


    @decorators.action(detail=False, methods=("get",), url_path="employee-pay-report")
    def employee_pay_report(self, request):
        """إعداد تقرير مدفوعات الموظفين للفترة المحددة."""
        company_raw = request.query_params.get("company", "")
        date_from_raw = request.query_params.get("date_from", "")
        date_to_raw = request.query_params.get("date_to", "")
        if not company_raw.isdigit():
            raise ValidationError({"company": "حدد الشركة المطلوبة."})
        company_id = int(company_raw)
        if not can_access_company(request.user, company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الشركة.")
        date_from = parse_date(date_from_raw)
        date_to = parse_date(date_to_raw)
        if not date_from or not date_to or date_from > date_to:
            raise ValidationError({"date_from": "حدد فترة صحيحة بصيغة YYYY-MM-DD."})

        cycles = self.get_queryset().filter(
            company_id=company_id,
            status__in=(
                PayrollCycle.Status.POSTED,
                PayrollCycle.Status.PAYMENT_PENDING,
                PayrollCycle.Status.PAID,
            ),
            period_start__lte=date_to,
            period_end__gte=date_from,
        )
        branch_raw = request.query_params.get("branch")
        if branch_raw:
            if not branch_raw.isdigit() or not can_access_branch(request.user, int(branch_raw), company_id):
                raise PermissionDenied("لا تملك صلاحية الوصول إلى الفرع المحدد.")
            cycles = cycles.filter(results__employee__branch_id=int(branch_raw)).distinct()

        results = PayrollResult.objects.filter(cycle__in=cycles).select_related(
            "employee", "employee__branch", "employee__department", "cycle"
        ).prefetch_related("lines")
        if branch_raw:
            results = results.filter(employee__branch_id=int(branch_raw))
        results = results.order_by("employee__code", "cycle__period_start")
        employees = {}
        totals = {
            "earnings": Decimal("0"), "employee_deductions": Decimal("0"),
            "employer_share": Decimal("0"), "net_pay": Decimal("0"), "company_cost": Decimal("0"),
        }
        for result in results:
            employee = result.employee
            row = employees.setdefault(employee.pk, {
                "employee_id": employee.pk,
                "employee_code": employee.code,
                "employee_name": employee.full_name_ar,
                "branch": employee.branch.name_ar,
                "department": employee.department.name_ar if employee.department_id else "",
                "currency": employee.currency.code,
                "cycle_count": 0,
                "earnings": Decimal("0"),
                "employee_deductions": Decimal("0"),
                "employer_share": Decimal("0"),
                "net_pay": Decimal("0"),
                "company_cost": Decimal("0"),
                "components": {},
                "periods": [],
            })
            row["cycle_count"] += 1
            row["earnings"] += result.earnings_total
            row["employee_deductions"] += result.employee_deductions_total
            row["employer_share"] += result.employer_share_total
            row["net_pay"] += result.net_pay
            row["company_cost"] += result.company_cost
            row["periods"].append({
                "cycle_id": result.cycle_id,
                "period_start": result.cycle.period_start,
                "period_end": result.cycle.period_end,
                "status": result.cycle.status,
                "net_pay": result.net_pay,
            })
            for line in result.lines.all():
                if line.code_snapshot in ("EMPLOYER_SHARE_DISPLAY", "EMPLOYER_SHARE_OFFSET"):
                    continue
                key = (line.code_snapshot, line.name_snapshot, line.kind_snapshot)
                component = row["components"].setdefault(key, Decimal("0"))
                row["components"][key] = component + line.amount
            for key in totals:
                source_key = "employee_deductions" if key == "employee_deductions" else key
                totals[key] += getattr(result, {
                    "earnings": "earnings_total",
                    "employee_deductions": "employee_deductions_total",
                    "employer_share": "employer_share_total",
                    "net_pay": "net_pay",
                    "company_cost": "company_cost",
                }[source_key])

        rows = []
        for row in employees.values():
            row["components"] = [
                {"code": code, "name": name, "kind": kind, "amount": amount}
                for (code, name, kind), amount in sorted(row["components"].items())
            ]
            rows.append(row)
        return response.Response({
            "company": company_id,
            "date_from": date_from,
            "date_to": date_to,
            "branch": int(branch_raw) if branch_raw else None,
            "currency_note": "المجاميع تعرض بعملة كل موظف؛ لا يتم جمع عملات مختلفة في إجمالي موحد.",
            "employees": rows,
            "totals": totals if len({row["currency"] for row in rows}) <= 1 else None,
            "employee_count": len(rows),
        })

class LoanInstallmentViewSet(viewsets.ModelViewSet):
    queryset = LoanInstallment.objects.select_related("loan", "loan__employee", "loan__employee__company").all()
    serializer_class = LoanInstallmentSerializer
    permission_classes = (IsAuthenticated,)
    http_method_names = ("get", "post", "head", "options")
    filterset_fields = ("loan", "loan__employee", "loan__employee__company", "due_date", "status")

    def get_queryset(self):
        """تجهيز السجلات المسموح بعرضها بعد تطبيق نطاق الشركة والفرع."""
        qs = self.queryset
        if not self.request.user.is_superuser or is_platform_owner(self.request.user):
            qs = qs.filter(
                loan__employee__company__in=accessible_companies(self.request.user),
                loan__employee__branch__in=accessible_branches(self.request.user),
            )
        company_id = self.request.query_params.get("company")
        if company_id:
            qs = qs.filter(loan__employee__company_id=company_id)
        return qs

    def perform_create(self, serializer):
        """حفظ سجل جديد بعد التحقق من الصلاحيات والعلاقات المرتبطة."""
        loan = serializer.validated_data["loan"]
        if not can_access_company(self.request.user, loan.employee.company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى الشركة.")
        if not has_company_role(self.request.user, loan.employee.company_id, ("admin", "accountant")):
            raise PermissionDenied("لا تملك صلاحية إنشاء جدول أقساط السلفة.")
        if not can_access_branch(self.request.user, loan.employee.branch_id, loan.employee.company_id):
            raise PermissionDenied("لا تملك صلاحية الوصول إلى فرع الموظف.")
        serializer.save()

