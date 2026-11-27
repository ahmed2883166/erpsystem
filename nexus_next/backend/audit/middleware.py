"""وسيط تسجيل الطلبات والأحداث الأمنية المهمة دون تخزين بيانات اعتماد حساسة."""

import ipaddress
import json
import logging
import uuid

from django.urls import Resolver404, resolve

from companies.access import is_platform_owner
from companies.models import Branch, Company
from .models import AuditEvent

logger = logging.getLogger(__name__)
_ACTIONS = {
    "POST": AuditEvent.Action.CREATE,
    "PUT": AuditEvent.Action.UPDATE,
    "PATCH": AuditEvent.Action.UPDATE,
    "DELETE": AuditEvent.Action.DELETE,
}
_ACTION_ENDPOINTS = {"post", "reverse", "dispose", "depreciate", "approve", "reject", "transfer", "confirm", "cancel", "reconcile", "reopen"}


def _numeric_id(value):
    """تطبيع قيمة المعرّف الرقمي قبل تسجيل مرجع التدقيق."""
    if isinstance(value, dict):
        value = value.get("id") or value.get("pk")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _json_object(raw):
    """قراءة نص JSON وإرجاع كائن آمن عند صلاحية المحتوى."""
    if not raw or len(raw) > 65536:
        return {}
    try:
        value = json.loads(raw.decode("utf-8"))
        return value if isinstance(value, dict) else {}
    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
        return {}


def _target_before_request(request):
    """Capture ownership before a DELETE removes the target row."""
    try:
        match = resolve(request.path_info)
    except Resolver404:
        return None, None, None
    view_class = getattr(match.func, "cls", None)
    queryset = getattr(view_class, "queryset", None)
    serializer_class = getattr(view_class, "serializer_class", None)
    model = getattr(queryset, "model", None) or getattr(
        getattr(serializer_class, "Meta", None), "model", None
    )
    object_id = _numeric_id(match.kwargs.get("pk") or match.kwargs.get("id"))
    if model is None or object_id is None:
        return object_id, None, None
    instance = model._default_manager.filter(pk=object_id).first()
    if instance is None:
        return object_id, None, None
    if isinstance(instance, Company):
        return object_id, instance.pk, None
    company_id = getattr(instance, "company_id", None)
    branch_id = getattr(instance, "branch_id", None)
    if company_id is None and branch_id:
        branch = Branch.objects.filter(pk=branch_id).only("company_id").first()
        company_id = branch.company_id if branch else None
    return object_id, company_id, branch_id


class AuditTrailMiddleware:
    """Record API write attempts without persisting request/response payloads."""

    def __init__(self, get_response):
        """تهيئة وسيط سجل التدقيق وربطه بمعالج الطلب التالي."""
        self.get_response = get_response

    def __call__(self, request):
        """تسجيل أثر الطلب والتغيرات الناتجة عنه وفق قواعد التدقيق."""
        method = request.method.upper()
        is_global_superuser_read = (
            method == "GET"
            and request.path.startswith(("/api/", "/admin/"))
            and getattr(getattr(request, "user", None), "is_authenticated", False)
            and getattr(request.user, "is_superuser", False)
            and not is_platform_owner(request.user)
        )
        should_audit = (request.path.startswith("/api/") and method in _ACTIONS) or is_global_superuser_read
        try:
            prior_object_id, prior_company_id, prior_branch_id = (
                _target_before_request(request) if should_audit else (None, None, None)
            )
        except Exception:
            # Audit context lookup must never prevent the business request from running.
            logger.exception("Could not resolve the target before request")
            prior_object_id, prior_company_id, prior_branch_id = None, None, None
        raw_body = b""
        if should_audit and "json" in request.META.get("CONTENT_TYPE", "").lower():
            try:
                content_length = int(request.META.get("CONTENT_LENGTH") or 0)
                if content_length <= 65536:
                    # Cache the body before DRF consumes the request stream.
                    raw_body = request.body
            except (ValueError, OSError):
                raw_body = b""
        response = self.get_response(request)
        if not should_audit:
            return response

        try:
            payload = {}
            if raw_body:
                payload = _json_object(raw_body)

            requested_company_id = _numeric_id(
                request.GET.get("company") or payload.get("company") or payload.get("company_id")
            )
            requested_branch_id = _numeric_id(
                request.GET.get("branch") or payload.get("branch") or payload.get("branch_id")
            )
            # Existing target ownership is authoritative; request-controlled values
            # may describe a different tenant and must never override it.
            if prior_object_id is not None:
                company_id = prior_company_id
                branch_id = prior_branch_id
            else:
                company_id = requested_company_id
                branch_id = requested_branch_id
            company = Company.objects.filter(pk=company_id).first() if company_id else None
            branch = Branch.objects.select_related("company").filter(pk=branch_id).first() if branch_id else None
            if branch and company and branch.company_id != company.pk:
                branch = None
            if branch and company is None:
                company = branch.company

            response_data = {}
            content_type = response.get("Content-Type", "")
            if "json" in content_type.lower() and not getattr(response, "streaming", False):
                response_data = _json_object(response.content)
            object_id = _numeric_id(response_data.get("id")) or prior_object_id
            path_parts = [part for part in request.path.rstrip("/").split("/") if part]
            if object_id is None and path_parts:
                object_id = _numeric_id(path_parts[-1])

            action = _ACTIONS.get(method, AuditEvent.Action.ACTION)
            if method == "POST" and path_parts and path_parts[-1].lower() in _ACTION_ENDPOINTS:
                action = AuditEvent.Action.ACTION

            user = getattr(request, "user", None)
            actor = user if getattr(user, "is_authenticated", False) else None
            request_id = request.META.get("HTTP_X_REQUEST_ID", "")
            try:
                request_id = uuid.UUID(request_id) if request_id else uuid.uuid4()
            except (ValueError, TypeError, AttributeError):
                request_id = uuid.uuid4()

            ip_address = request.META.get("REMOTE_ADDR")
            try:
                ip_address = str(ipaddress.ip_address(ip_address)) if ip_address else None
            except ValueError:
                ip_address = None

            resource = request.path.removeprefix("/api/").strip("/")[:180] or "api"
            AuditEvent.objects.create(
                request_id=request_id,
                actor=actor,
                company=company,
                branch=branch,
                action=action,
                resource=resource,
                object_id=str(object_id or ""),
                http_method=method,
                status_code=response.status_code,
                succeeded=response.status_code < 400,
                ip_address=ip_address,
                user_agent=request.META.get("HTTP_USER_AGENT", "")[:300],
                metadata=(
                    {"status_code": response.status_code, "superuser_api_read": True,
                     "global_company_branch_visibility_applies": True}
                    if is_global_superuser_read else {"status_code": response.status_code}
                ),
            )
        except Exception:
            # Auditing must never turn a completed business request into a 500.
            logger.exception("Could not persist NEXUS audit event")
        return response
