import os
import re
from pathlib import Path, PureWindowsPath

from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import BackupConfiguration


def _config():
    return BackupConfiguration.objects.first() or BackupConfiguration.objects.create()


def _serialize(config, can_manage):
    return {
        "backup_path": config.backup_path,
        "schedule": config.schedule,
        "enabled": config.enabled,
        "daily_retention_days": config.daily_retention_days,
        "monthly_retention_months": config.monthly_retention_months,
        "updated_at": config.updated_at,
        "can_manage": can_manage,
        "path_configured": bool(config.backup_path),
    }


class BackupConfigurationView(APIView):
    permission_classes = (IsAuthenticated,)

    def get(self, request):
        config = _config()
        return Response(_serialize(config, request.user.is_superuser))

    def patch(self, request):
        if not request.user.is_superuser:
            raise PermissionDenied("تغيير إعدادات النسخ الاحتياطي متاح لمدير النظام الأعلى فقط.")
        config = _config()
        allowed = {"backup_path", "schedule", "enabled", "daily_retention_days", "monthly_retention_months"}
        unknown = set(request.data) - allowed
        if unknown:
            raise ValidationError({"detail": "توجد حقول غير مسموح بتعديلها."})
        if "backup_path" in request.data:
            raw = str(request.data["backup_path"]).strip()
            if not raw:
                raise ValidationError({"backup_path": "حدد مجلد النسخ الاحتياطي."})
            win = PureWindowsPath(raw)
            if not win.is_absolute() or not win.drive or win.drive.upper().startswith("C:"):
                raise ValidationError({"backup_path": "يجب اختيار مسار مطلق على قرص غير C، مثل D:\\NEXUS_Backups."})
            if any(part in (".", "..") for part in win.parts):
                raise ValidationError({"backup_path": "المسار يحتوي على أجزاء غير مسموح بها."})
            # Validate on the machine hosting Django; never create arbitrary paths from a request.
            candidate = Path(raw)
            if os.name == "nt":
                if not candidate.exists() or not candidate.is_dir():
                    raise ValidationError({"backup_path": "المجلد غير موجود على جهاز الخادم."})
                if not os.access(candidate, os.W_OK):
                    raise ValidationError({"backup_path": "لا توجد صلاحية كتابة في المجلد المحدد."})
            config.backup_path = str(win)
        if "schedule" in request.data:
            if request.data["schedule"] not in BackupConfiguration.Schedule.values:
                raise ValidationError({"schedule": "نوع الجدولة غير صحيح."})
            config.schedule = request.data["schedule"]
        if "enabled" in request.data:
            if not isinstance(request.data["enabled"], bool):
                raise ValidationError({"enabled": "القيمة يجب أن تكون نعم أو لا."})
            config.enabled = request.data["enabled"]
        for field, low, high in (("daily_retention_days", 1, 3650), ("monthly_retention_months", 1, 120)):
            if field in request.data:
                try:
                    value = int(request.data[field])
                except (TypeError, ValueError):
                    raise ValidationError({field: "أدخل رقمًا صحيحًا."})
                if not low <= value <= high:
                    raise ValidationError({field: f"القيمة يجب أن تكون بين {low} و{high}."})
                setattr(config, field, value)
        config.updated_by = request.user
        config.save()
        return Response(_serialize(config, True))
