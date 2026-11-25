"""نماذج سجل التدقيق غير القابل للتعديل لتوثيق أحداث المستخدمين والعمليات."""

import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone


class ImmutableAuditQuerySet(models.QuerySet):
    """يوفر مجموعة استعلامات تمنع تعديل أو حذف أحداث التدقيق."""
    def update(self, **kwargs):
        raise TypeError("Audit events are immutable.")

    def delete(self):
        raise TypeError("Audit events are immutable.")


class AuditEvent(models.Model):
    """يحفظ أثر العملية وهوية المستخدم والهدف والتوقيت والبيانات المسجلة."""
    class Action(models.TextChoices):
        CREATE = "create", "إنشاء"
        UPDATE = "update", "تعديل"
        DELETE = "delete", "حذف"
        ACTION = "action", "إجراء"

    request_id = models.UUIDField(default=uuid.uuid4, editable=False, db_index=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.PROTECT, related_name="audit_events",
    )
    company = models.ForeignKey(
        "companies.Company", null=True, blank=True,
        on_delete=models.PROTECT, related_name="audit_events",
    )
    branch = models.ForeignKey(
        "companies.Branch", null=True, blank=True,
        on_delete=models.PROTECT, related_name="audit_events",
    )
    action = models.CharField(max_length=8, choices=Action.choices, db_index=True)
    resource = models.CharField(max_length=180, db_index=True)
    object_id = models.CharField(max_length=80, blank=True)
    http_method = models.CharField(max_length=8)
    status_code = models.PositiveSmallIntegerField()
    succeeded = models.BooleanField(default=False, db_index=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=300, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False, db_index=True)

    objects = ImmutableAuditQuerySet.as_manager()

    class Meta:
        ordering = ("-created_at", "-pk")
        indexes = [
            models.Index(fields=("company", "created_at"), name="audit_company_created_idx"),
            models.Index(fields=("actor", "created_at"), name="audit_actor_created_idx"),
            models.Index(fields=("resource", "created_at"), name="audit_resource_created_idx"),
        ]
        verbose_name = "حدث تدقيق"
        verbose_name_plural = "أحداث التدقيق"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise TypeError("Audit events are immutable.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise TypeError("Audit events are immutable.")

    def __str__(self):
        return f"{self.created_at:%Y-%m-%d %H:%M:%S} - {self.resource} - {self.action}"
