import logging

from django.contrib.admin.models import ADDITION, CHANGE, DELETION, LogEntry
from django.contrib.auth.signals import user_logged_in, user_logged_out, user_login_failed
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import AuditEvent

logger = logging.getLogger(__name__)


def _safe_create_audit_event(**kwargs):
    """تسجيل حدث تدقيق بأمان مع منع فشل العملية الأصلية بسبب سجل التدقيق."""
    try:
        AuditEvent.objects.create(**kwargs)
    except Exception:
        # Audit outages must not break authentication or Django Admin actions.
        logger.exception("Could not persist authentication/admin audit event")


def _request_details(request):
    """استخراج بيانات الطلب غير الحساسة اللازمة لسجل التدقيق."""
    if request is None:
        return None, ""
    return request.META.get("REMOTE_ADDR") or None, request.META.get("HTTP_USER_AGENT", "")[:300]


def _record_auth_event(*, request, user, event, succeeded, status_code):
    """تسجيل حدث مصادقة مع النتيجة ورمز الاستجابة والفاعل."""
    ip_address, user_agent = _request_details(request)
    _safe_create_audit_event(
        actor=user,
        action=AuditEvent.Action.ACTION,
        resource=f"authentication.{event}",
        object_id=str(getattr(user, "pk", "") or ""),
        http_method="AUTH",
        status_code=status_code,
        succeeded=succeeded,
        ip_address=ip_address,
        user_agent=user_agent,
        metadata={"event": event},
    )


@receiver(user_logged_in)
def audit_login(sender, request, user, **kwargs):
    """تسجيل نجاح تسجيل الدخول في سجل التدقيق."""
    _record_auth_event(
        request=request, user=user, event="login",
        succeeded=True, status_code=200,
    )


@receiver(user_logged_out)
def audit_logout(sender, request, user, **kwargs):
    """تسجيل خروج المستخدم في سجل التدقيق."""
    _record_auth_event(
        request=request, user=user, event="logout",
        succeeded=True, status_code=200,
    )


@receiver(user_login_failed)
def audit_failed_login(sender, credentials, request, **kwargs):
    """تسجيل محاولة دخول فاشلة دون حفظ كلمة المرور."""
    # Never persist submitted credentials or attempted usernames.
    _record_auth_event(
        request=request, user=None, event="login_failed",
        succeeded=False, status_code=401,
    )


@receiver(post_save, sender=LogEntry)
def audit_django_admin_change(sender, instance, created, **kwargs):
    """تسجيل التغييرات التي تتم من لوحة إدارة Django."""
    if not created:
        return
    action_by_flag = {
        ADDITION: AuditEvent.Action.CREATE,
        CHANGE: AuditEvent.Action.UPDATE,
        DELETION: AuditEvent.Action.DELETE,
    }
    action = action_by_flag.get(instance.action_flag, AuditEvent.Action.ACTION)
    if instance.content_type_id is None:
        return
    model = instance.content_type.model_class()
    company = None
    branch = None
    if model is not None and instance.action_flag != DELETION:
        obj = model._default_manager.filter(pk=instance.object_id).first()
        if obj is not None:
            from companies.models import Branch, Company
            if isinstance(obj, Company):
                company = obj
            elif isinstance(obj, Branch):
                branch = obj
                company = obj.company
            else:
                company_id = getattr(obj, "company_id", None)
                branch_id = getattr(obj, "branch_id", None)
                if branch_id:
                    branch = Branch.objects.filter(pk=branch_id).first()
                if company_id is None and branch is not None:
                    company_id = branch.company_id
                if company_id:
                    company = Company.objects.filter(pk=company_id).first()
    model_name = instance.content_type.model
    app_label = instance.content_type.app_label
    _safe_create_audit_event(
        actor=instance.user,
        company=company,
        branch=branch,
        action=action,
        resource=f"django_admin.{app_label}.{model_name}"[:180],
        object_id=str(instance.object_id)[:80],
        http_method="ADMIN",
        status_code=200,
        succeeded=True,
        metadata={"event": "admin_change", "action_flag": instance.action_flag},
    )
