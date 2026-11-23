from django.db.models import F, Q
from rest_framework.exceptions import ValidationError

from .models import Branch, Company, CompanyMembership


def parse_company_param(request):
    """قراءة مرشح الشركة من الاستعلام وإرجاع 400 عند تمرير معرّف غير صالح."""
    raw = request.query_params.get("company")
    if raw in (None, ""):
        return None
    try:
        company_id = int(raw)
    except (TypeError, ValueError):
        raise ValidationError({"company": "معرّف الشركة غير صالح."})
    if company_id <= 0:
        raise ValidationError({"company": "معرّف الشركة غير صالح."})
    return company_id


def accessible_companies(user):
    """إرجاع الشركات التي يحق للمستخدم الوصول إليها وفق عضوياته وصلاحياته."""
    if is_platform_owner(user):
        return Company.objects.none()
    if user.is_superuser:
        return Company.objects.filter(is_active=True)
    return Company.objects.filter(
        memberships__user=user,
        memberships__is_active=True,
        is_active=True,
    ).distinct()


def accessible_branches(user, company_id=None):
    """إرجاع الفروع المتاحة للمستخدم مع إمكانية حصر النتائج في شركة محددة."""
    if is_platform_owner(user):
        return Branch.objects.none()
    if user.is_superuser:
        qs = Branch.objects.filter(is_active=True, company__is_active=True)
        return qs.filter(company_id=company_id) if company_id else qs

    memberships = CompanyMembership.objects.filter(
        user=user, is_active=True, company__is_active=True
    )
    if company_id:
        memberships = memberships.filter(company_id=company_id)
    all_company_ids = memberships.filter(all_branches=True).values("company_id")
    explicit_branch_ids = memberships.filter(
        all_branches=False, branches__company_id=F("company_id")
    ).values("branches__id")
    return Branch.objects.filter(
        Q(company_id__in=all_company_ids) | Q(id__in=explicit_branch_ids),
        is_active=True,
        company__is_active=True,
    ).distinct()


def can_access_company(user, company_id):
    """التحقق من امتلاك المستخدم حق الوصول إلى الشركة المحددة."""
    return not is_platform_owner(user) and (user.is_superuser or accessible_companies(user).filter(pk=company_id).exists())


def has_company_wide_branch_access(user, company_id):
    """التحقق من أن المستخدم يرى كل الفروع النشطة قبل كشف أرصدة حسابات الشركة العامة."""
    if is_platform_owner(user):
        return False
    if user.is_superuser:
        return True
    active_branch_ids = list(
        Branch.objects.filter(company_id=company_id, is_active=True).values_list("id", flat=True)
    )
    if not active_branch_ids:
        return False
    accessible_ids = accessible_branches(user, company_id).filter(
        pk__in=active_branch_ids
    ).count()
    return accessible_ids == len(active_branch_ids)


def can_access_branch(user, branch_id, company_id=None):
    """التحقق من صلاحية المستخدم على الفرع مع مراعاة نطاق الشركة."""
    return accessible_branches(user, company_id).filter(pk=branch_id).exists()


def has_company_role(user, company_id, roles):
    """التحقق من أن دور عضوية المستخدم ضمن الأدوار المسموح بها."""
    if is_platform_owner(user):
        return False
    if user.is_superuser:
        return True
    return CompanyMembership.objects.filter(
        user=user,
        company_id=company_id,
        is_active=True,
        company__is_active=True,
        role__in=roles,
    ).exists()


def is_platform_owner(user):
    """التحقق من هوية مالك المنصة دون منح صلاحيات تشغيلية داخل الشركات."""
    if not user or not user.is_authenticated or not user.is_active:
        return False
    from .models import PlatformOwner
    return PlatformOwner.objects.filter(user=user, is_active=True).exists()



def resolve_warehouse_branch(company, warehouse):
    """توحيد الفرع المحاسبي للمخزن بين التحقق والترحيل."""
    branch = warehouse.branch if warehouse.branch_id else None
    if branch:
        return branch
    branch = Branch.objects.filter(
        company=company, is_active=True, is_head_office=True,
    ).first()
    if not branch:
        branch = Branch.objects.filter(
            company=company, is_active=True,
        ).order_by("code", "pk").first()
    return branch


def ensure_fiscal_period_open(fiscal_year, target_date):
    """منع التعديل أو الترحيل داخل فترة شهرية مغلقة مع الحفاظ على السنوات القديمة غير المولدة."""
    from django.core.exceptions import ValidationError
    from .models import FiscalPeriod

    periods = FiscalPeriod.objects.filter(fiscal_year_id=fiscal_year.pk)
    if not periods.exists():
        return None
    period = periods.filter(start_date__lte=target_date, end_date__gte=target_date).first()
    if not period:
        raise ValidationError("لا توجد فترة شهرية معتمدة تغطي تاريخ المستند داخل هذه السنة المالية.")
    if period.status != FiscalPeriod.Status.OPEN:
        raise ValidationError(f"الفترة المالية {period.name} مغلقة؛ لا يمكن التعديل أو الترحيل بأثر رجعي.")
    return period
