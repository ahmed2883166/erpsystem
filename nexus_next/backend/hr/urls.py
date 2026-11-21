from rest_framework.routers import DefaultRouter

from .api import (
    AttendanceViewSet, DepartmentViewSet, EmployeeComponentViewSet,
    EmployeeLoanViewSet, EmployeeViewSet, JobPositionViewSet,
    LeaveRequestViewSet, EmployeeSalaryHistoryViewSet, PayrollComponentViewSet, PayrollCycleViewSet,
    PayrollRuleSetViewSet,
)

router = DefaultRouter()
router.register("departments", DepartmentViewSet, basename="hr-department")
router.register("positions", JobPositionViewSet, basename="hr-position")
router.register("employees", EmployeeViewSet, basename="hr-employee")
router.register("payroll-components", PayrollComponentViewSet, basename="payroll-component")
router.register("employee-components", EmployeeComponentViewSet, basename="employee-component")
router.register("attendance", AttendanceViewSet, basename="hr-attendance")
router.register("leave-requests", LeaveRequestViewSet, basename="hr-leave")
router.register("employee-loans", EmployeeLoanViewSet, basename="hr-loan")
router.register("salary-history", EmployeeSalaryHistoryViewSet, basename="hr-salary-history")
router.register("payroll-rule-sets", PayrollRuleSetViewSet, basename="payroll-rule-set")
router.register("payroll-cycles", PayrollCycleViewSet, basename="payroll-cycle")

urlpatterns = router.urls
