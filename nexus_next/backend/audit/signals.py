from django.contrib.admin.models import LogEntry
from django.contrib.auth.signals import user_logged_in, user_logged_out, user_login_failed
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import AuditEvent


def _request_details(request):
    if request is None:
        return None, ""
    return request.META.get("REMOTE_ADDR") or None, request.META.get("HTTP_USER_AGENT", "")[:300]


def _record_auth_event(*, request, user, event, succeeded, status_code):
    ip_address, user_agent = _request_details(request)
    AuditEvent.objects.create(
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
    _record_auth_event(
        request=request, user=user, event="login",
        succeeded=True, status_code=200,
    )


@receiver(user_logged_out)
def audit_logout(sender, request, user, **kwargs):
    _record_auth_event(
        request=request, user=user, event="logout",
        succeeded=True, status_code=200,
    )


@receiver(user_login_failed)
def audit_failed_login(sender, credentials, request, **kwargs):
    # Never persist submitted credentials or attempted usernames.
    _record_auth_event(
        request=request, user=None, event="login_failed",
        succeeded=False, status_code=401,
    )


@receiver(post_save, sender=LogEntry)
def audit_django_admin_change(sender, instance, created, **kwargs):
    if not created:
        return
    action_by_flag = {
        LogEntry.ADDITION: AuditEvent.Action.CREATE,
        LogEntry.CHANGE: AuditEvent.Action.UPDATE,
        LogEntry.DELETION: AuditEvent.Action.DELETE,
    }
    action = action_by_flag.get(instance.action_flag, AuditEvent.Action.ACTION)
    model_name = instance.content_type.model
    app_label = instance.content_type.app_label
    AuditEvent.objects.create(
        actor=instance.user,
        action=action,
        resource=f"django_admin.{app_label}.{model_name}"[:180],
        object_id=str(instance.object_id)[:80],
        http_method="ADMIN",
        status_code=200,
        succeeded=True,
        metadata={"event": "admin_change", "action_flag": instance.action_flag},
    )
