"""نماذج الإعدادات التشغيلية وسجل تنفيذ النسخ الاحتياطي للنظام."""

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models


class BackupConfiguration(models.Model):
    """يحفظ جدول النسخ الاحتياطي وسياسة الاحتفاظ بالنسخ."""
    class Schedule(models.TextChoices):
        DAILY = "daily", "يومي"
        WEEKLY = "weekly", "أسبوعي"
        MONTHLY = "monthly", "شهري"

    backup_path = models.CharField(max_length=500, blank=True)
    schedule = models.CharField(max_length=10, choices=Schedule.choices, default=Schedule.DAILY)
    enabled = models.BooleanField(default=True)
    daily_retention_days = models.PositiveSmallIntegerField(default=30, validators=[MinValueValidator(1)])
    monthly_retention_months = models.PositiveSmallIntegerField(default=12, validators=[MinValueValidator(1)])
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "إعدادات النسخ الاحتياطي"
        verbose_name_plural = "إعدادات النسخ الاحتياطي"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)


class BackupRun(models.Model):
    """يسجل نتيجة محاولة النسخ الاحتياطي وملفها وحالتها."""
    class Status(models.TextChoices):
        SUCCESS = "success", "ناجح"
        FAILURE = "failure", "فشل"

    status = models.CharField(max_length=10, choices=Status.choices, db_index=True)
    backup_file = models.CharField(max_length=500, blank=True)
    size_bytes = models.BigIntegerField(default=0)
    details = models.TextField(blank=True)
    started_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ("-created_at", "-pk")
        verbose_name = "عملية نسخ احتياطي"
        verbose_name_plural = "عمليات النسخ الاحتياطي"
