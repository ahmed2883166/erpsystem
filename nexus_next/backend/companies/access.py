from companies.models import CompanyMembership


def accessible_company_ids(user):
    if not user or not user.is_authenticated:
        return []
    if user.is_superuser or user.is_staff:
        return None
    return CompanyMembership.objects.filter(
        user=user, is_active=True, company__is_active=True
    ).values_list("company_id", flat=True)


def scope_company_queryset(queryset, user, field="company_id"):
    company_ids = accessible_company_ids(user)
    if company_ids is None:
        return queryset
    return queryset.filter(**{f"{field}__in": company_ids})


def user_can_access_company(user, company_id):
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser or user.is_staff:
        return True
    return CompanyMembership.objects.filter(
        user=user, company_id=company_id, is_active=True, company__is_active=True
    ).exists()
