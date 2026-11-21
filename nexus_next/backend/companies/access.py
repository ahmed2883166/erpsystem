from django.db.models import F, Q

from .models import Branch, Company, CompanyMembership


def accessible_companies(user):
    """إرجاع الشركات التي يحق للمستخدم الوصول إليها وفق عضوياته وصلاحياته."""
    if user.is_superuser:
        return Company.objects.filter(is_active=True)
    return Company.objects.filter(
        memberships__user=user,
        memberships__is_active=True,
        is_active=True,
    ).distinct()


def accessible_branches(user, company_id=None):
    """إرجاع الفروع المتاحة للمستخدم مع إمكانية حصر النتائج في شركة محددة."""
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
    return user.is_superuser or accessible_companies(user).filter(pk=company_id).exists()


def can_access_branch(user, branch_id, company_id=None):
    """التحقق من صلاحية المستخدم على الفرع مع مراعاة نطاق الشركة."""
    return accessible_branches(user, company_id).filter(pk=branch_id).exists()


def has_company_role(user, company_id, roles):
    """التحقق من أن دور عضوية المستخدم ضمن الأدوار المسموح بها."""
    if user.is_superuser:
        return True
    return CompanyMembership.objects.filter(
        user=user,
        company_id=company_id,
        is_active=True,
        company__is_active=True,
        role__in=roles,
    ).exists()
