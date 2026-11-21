from django.db.models import Q
from django.utils.dateparse import parse_date
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.exceptions import PermissionDenied, ValidationError

from companies.access import accessible_branches
from companies.models import CompanyMembership
from .models import AuditEvent


class AuditEventListView(APIView):
    permission_classes = (IsAuthenticated,)

    def get(self, request):
        qs = AuditEvent.objects.select_related("actor", "company", "branch")
        if not request.user.is_superuser:
            memberships = CompanyMembership.objects.filter(
                user=request.user, is_active=True,
                role__in=(CompanyMembership.Role.ADMIN, CompanyMembership.Role.ACCOUNTANT),
                company__is_active=True,
            )
            company_ids = memberships.values_list("company_id", flat=True)
            qs = qs.filter(company_id__in=company_ids).filter(
                Q(branch__isnull=True) | Q(branch__in=accessible_branches(request.user))
            ).order_by("-created_at", "-pk")

        company_raw = request.query_params.get("company")
        if company_raw:
            if not company_raw.isdigit():
                raise ValidationError({"company": "معرّف الشركة غير صحيح."})
            company_id = int(company_raw)
            if not request.user.is_superuser and not CompanyMembership.objects.filter(
                user=request.user, company_id=company_id, is_active=True,
                role__in=(CompanyMembership.Role.ADMIN, CompanyMembership.Role.ACCOUNTANT),
            ).exists():
                raise PermissionDenied("لا تملك صلاحية مراجعة سجل هذه الشركة.")
            qs = qs.filter(company_id=company_id)

        branch_raw = request.query_params.get("branch")
        if branch_raw:
            if not branch_raw.isdigit():
                raise ValidationError({"branch": "معرّف الفرع غير صحيح."})
            branch_id = int(branch_raw)
            if not request.user.is_superuser and not accessible_branches(
                request.user
            ).filter(pk=branch_id).exists():
                raise PermissionDenied("لا تملك صلاحية مراجعة سجل هذا الفرع.")
            qs = qs.filter(branch_id=branch_id)

        action = request.query_params.get("action")
        if action:
            if action not in AuditEvent.Action.values:
                raise ValidationError({"action": "نوع العملية غير صحيح."})
            qs = qs.filter(action=action)

        date_from = request.query_params.get("date_from")
        date_to = request.query_params.get("date_to")
        if date_from:
            parsed = parse_date(date_from)
            if not parsed:
                raise ValidationError({"date_from": "استخدم YYYY-MM-DD."})
            qs = qs.filter(created_at__date__gte=parsed)
        if date_to:
            parsed = parse_date(date_to)
            if not parsed:
                raise ValidationError({"date_to": "استخدم YYYY-MM-DD."})
            qs = qs.filter(created_at__date__lte=parsed)

        limit_raw = request.query_params.get("limit", "100")
        if not limit_raw.isdigit():
            raise ValidationError({"limit": "الحد يجب أن يكون رقمًا."})
        limit = min(max(int(limit_raw), 1), 200)
        events = qs[:limit]
        return Response({
            "count": len(events),
            "results": [{
                "id": event.pk,
                "request_id": str(event.request_id),
                "created_at": event.created_at,
                "actor": event.actor.get_full_name() or event.actor.username if event.actor else "مستخدم غير مسجل",
                "company_id": event.company_id,
                "company": event.company.name_ar if event.company else None,
                "branch_id": event.branch_id,
                "branch": event.branch.name_ar if event.branch else None,
                "action": event.action,
                "resource": event.resource,
                "object_id": event.object_id,
                "http_method": event.http_method,
                "status_code": event.status_code,
                "succeeded": event.succeeded,
                "ip_address": event.ip_address,
                "user_agent": event.user_agent,
                "metadata": event.metadata,
            } for event in events],
        })
