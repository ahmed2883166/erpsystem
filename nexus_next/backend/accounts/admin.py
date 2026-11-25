"""تخصيص شاشة إدارة مستخدمي النظام داخل لوحة Django الإدارية."""

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import User

@admin.register(User)
class NexusUserAdmin(UserAdmin):
    fieldsets = UserAdmin.fieldsets + (
        ("NEXUS", {"fields": ("display_name", "preferred_language", "theme_preference")}),
    )
    add_fieldsets = UserAdmin.add_fieldsets + (
        ("NEXUS", {"fields": ("display_name", "preferred_language")}),
    )
    list_display = ("username", "display_name", "email", "is_staff", "is_active")
