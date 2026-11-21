from django.contrib import admin
from .models import AuditEvent


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    list_display = ("created_at", "actor", "company", "branch", "action", "resource", "status_code", "succeeded")
    list_filter = ("action", "succeeded", "status_code", "company")
    search_fields = ("resource", "object_id", "actor__username", "request_id")
    readonly_fields = tuple(field.name for field in AuditEvent._meta.fields)
    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
