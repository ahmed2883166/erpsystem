import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path, PureWindowsPath

from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from django.db import transaction
from django.conf import settings
from django.contrib.auth.hashers import check_password
from django.utils import timezone
from datetime import timedelta
from audit.models import AuditEvent
from companies.access import is_platform_owner

from .backup_crypto import BackupCryptoError
from .models import BackupConfiguration, BackupRun
from .reset_service import reset_preview, reset_operational_data


logger = logging.getLogger(__name__)


def _safe_backup_audit_event(**kwargs):
    """Do not turn a completed backup into an API failure if audit storage is unavailable."""
    try:
        AuditEvent.objects.create(**kwargs)
    except Exception:
        logger.exception("Could not persist backup audit event")


def _powershell_executable():
    """Resolve the Windows PowerShell executable to a trusted absolute path."""
    system_root = Path(os.environ.get("SystemRoot", "C:/Windows"))
    candidates = [
        system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe",
        system_root / "Sysnative" / "WindowsPowerShell" / "v1.0" / "powershell.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)

    raise FileNotFoundError("Windows PowerShell executable was not found in System32 or Sysnative.")

_BACKUP_THREAD_LOCK = threading.Lock()


class BackupAlreadyRunning(Exception):
    """Raised when another backup operation already owns the host-wide lock."""


@contextmanager
def _backup_execution_lock():
    """Serialize backup execution across threads and worker processes on this host."""
    if not _BACKUP_THREAD_LOCK.acquire(blocking=False):
        raise BackupAlreadyRunning
    handle = None
    locked = False
    try:
        lock_path = Path(tempfile.gettempdir()) / "admrail-backup-operation.lock"
        handle = open(lock_path, "a+b")
        if os.name == "nt":
            import msvcrt
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise BackupAlreadyRunning from exc
        else:
            import fcntl
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise BackupAlreadyRunning from exc
        locked = True
        yield
    finally:
        if handle is not None:
            if locked:
                if os.name == "nt":
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
        _BACKUP_THREAD_LOCK.release()


def _config():
    """استرجاع إعدادات النسخ الاحتياطي التشغيلية."""
    return BackupConfiguration.objects.first() or BackupConfiguration.objects.create()


def _backup_encryption_ready():
    """تحقق من إمكانية فتح مفتاح النسخ المشفر على جهاز Windows الحالي."""
    try:
        from .backup_key import machine_backup_passphrase
        machine_backup_passphrase()
        return True
    except (BackupCryptoError, OSError, ImportError, RuntimeError, ValueError):
        return False


def _serialize(config, can_manage):
    """تحويل إعدادات النسخ الاحتياطي إلى بيانات آمنة للعرض في الواجهة."""
    return {
        "encryption_ready": _backup_encryption_ready(),
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
        """عرض حالة النسخ بصورة منقحة؛ تفاصيل المسارات والسجل لمدير النظام فقط."""
        if is_platform_owner(request.user):
            raise PermissionDenied("مالك المنصة لا يطّلع على إعدادات النسخ الاحتياطي الخاصة بالشركات.")
        can_manage = request.user.is_superuser
        config = _config()
        payload = _serialize(config, can_manage)
        if can_manage:
            payload["history"] = [{
                "id": run.pk,
                "status": run.status,
                "backup_file": run.backup_file,
                "size_bytes": run.size_bytes,
                "details": run.details,
                "created_at": run.created_at,
            } for run in BackupRun.objects.select_related("started_by")[:50]]
        else:
            # Do not disclose server filesystem paths or backup execution details.
            payload["backup_path"] = ""
            payload["history"] = []
        return Response(payload)

    def post(self, request):
        """تنفيذ إعادة الضبط التشغيلية بعد التحقق من الصلاحية والتأكيد المطلوب."""
        if not request.user.is_superuser or is_platform_owner(request.user):
            raise PermissionDenied("تنفيذ النسخ الاحتياطي متاح لمدير النظام الأعلى فقط.")
        config = _config()
        if not config.enabled or not config.backup_path:
            raise ValidationError({"detail": "فعّل النسخ وحدد مجلدًا خارجيًا أولًا."})
        if sys.platform != "win32":
            from rest_framework.exceptions import APIException
            raise APIException("تنفيذ النسخ الاحتياطي متاح على جهاز Windows المستضيف للنظام.")
        script = settings.BASE_DIR.parent / "scripts" / "backup-postgres.ps1"
        if not script.is_file():
            raise ValidationError({"detail": "ملف تشغيل النسخ الاحتياطي غير موجود."})
        try:
            with _backup_execution_lock():
                result = subprocess.run(
                    [_powershell_executable(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                    cwd=str(settings.BASE_DIR.parent),
                    capture_output=True,
                    text=True,
                    timeout=7200,
                    check=False,
                )
            output = (result.stdout or "")[-6000:]
            error = (result.stderr or "")[-2000:]
            match = re.search(r"Encrypted backup verified:\s*(.+)", output)
            backup_file = match.group(1).strip() if match else ""
            file_path = Path(backup_file) if backup_file else None
            success = result.returncode == 0 and file_path is not None and file_path.is_file() and file_path.stat().st_size >= 1024
            details = output if success else (error or output or "تعذر التحقق من ملف النسخة الاحتياطية.")
            run = None
            if success:
                run = BackupRun.objects.filter(
                    status=BackupRun.Status.SUCCESS,
                    backup_file__iexact=str(file_path),
                ).first()
            if run:
                run.started_by = request.user
                run.details = details
                run.size_bytes = file_path.stat().st_size
                run.save(update_fields=["started_by", "details", "size_bytes"])
            else:
                run = BackupRun.objects.create(
                    status=BackupRun.Status.SUCCESS if success else BackupRun.Status.FAILURE,
                    backup_file=str(file_path) if success else "",
                    size_bytes=file_path.stat().st_size if success else 0,
                    details=details,
                    started_by=request.user,
                )
            _safe_backup_audit_event(
                actor=request.user, action=AuditEvent.Action.ACTION,
                resource="core.BackupRun", object_id=str(run.pk),
                http_method="POST", status_code=200 if success else 500,
                succeeded=success, metadata={"status": run.status, "backup_file": run.backup_file},
            )
            if not success:
                return Response({"detail": "فشل النسخ أو التحقق من الملف.", "run_id": run.pk, "details": details}, status=502)
            return Response({
                "id": run.pk, "status": run.status, "backup_file": run.backup_file,
                "size_bytes": run.size_bytes, "created_at": run.created_at,
                "detail": "تم إنشاء النسخة والتحقق من سلامة الأرشيف.",
            }, status=201)
        except BackupAlreadyRunning:
            return Response(
                {"detail": "توجد عملية نسخ احتياطي أخرى قيد التنفيذ؛ انتظر انتهاءها ثم أعد المحاولة."},
                status=409,
            )
        except subprocess.TimeoutExpired:
            run = BackupRun.objects.create(status=BackupRun.Status.FAILURE, details="انتهت مهلة النسخ الاحتياطي بعد ساعتين.", started_by=request.user)
            _safe_backup_audit_event(actor=request.user, action=AuditEvent.Action.ACTION, resource="core.BackupRun", object_id=str(run.pk), http_method="POST", status_code=500, succeeded=False, metadata={"status": run.status})
            return Response({"detail": "انتهت مهلة النسخ الاحتياطي؛ راجع سجل التشغيل."}, status=504)

    @transaction.atomic
    def patch(self, request):
        """تحديث إعدادات النسخ الاحتياطي مع الحفاظ على الحقول غير المرسلة."""
        if not request.user.is_superuser or is_platform_owner(request.user):
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
            # Revalidate only when the destination actually changes; unchanged
            # stored paths must remain editable for schedule/retention settings.
            current_path = PureWindowsPath(config.backup_path) if config.backup_path else None
            if current_path != win:
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
        _safe_backup_audit_event(
            actor=request.user,
            action=AuditEvent.Action.UPDATE,
            resource="core.BackupConfiguration",
            object_id=str(config.pk),
            http_method="PATCH",
            status_code=200,
            succeeded=True,
            metadata={
                "changed_fields": sorted(request.data.keys()),
                "backup_path": config.backup_path,
                "schedule": config.schedule,
                "enabled": config.enabled,
            },
        )
        return Response(_serialize(config, True))



class OperationalResetPreviewView(APIView):
    permission_classes = (IsAuthenticated,)

    def get(self, request):
        """عرض معاينة عملية إعادة الضبط والبيانات التي ستتأثر دون تنفيذها."""
        if not request.user.is_superuser or is_platform_owner(request.user):
            raise PermissionDenied("معاينة التصفير متاحة لمدير النظام الأعلى فقط.")
        counts = reset_preview()
        latest = BackupRun.objects.filter(status=BackupRun.Status.SUCCESS).first()
        backup_ready = bool(
            latest and latest.created_at >= timezone.now() - timedelta(hours=24)
            and latest.backup_file and Path(latest.backup_file).is_file()
            and Path(latest.backup_file).stat().st_size >= 1024
        )
        return Response({
            "counts": counts,
            "total_records": sum(counts.values()),
            "backup_ready": backup_ready,
            "last_backup_at": latest.created_at if latest else None,
            "preserved": [
                "الشركات والفروع والمستخدمون والصلاحيات",
                "دليل الحسابات ومراكز التكلفة وأكواد الضرائب",
                "العملاء والموردون والأصناف والمخازن",
                "الموظفون وإعدادات الموارد البشرية",
                "إعدادات النظام وسجل التدقيق",
            ],
            "removed": [
                "القيود وسطورها والفواتير والتحصيلات والمدفوعات",
                "حركات المخزون والأرصدة وطبقات التكلفة",
                "التسويات البنكية وحركات الخزينة",
                "سجل الأصول الثابتة والإهلاكات",
                "الحضور والإجازات والسلف وسجلات المرتبات",
                "طلبات الموافقات المرتبطة بالحركات",
            ],
        })


class OperationalResetView(APIView):
    permission_classes = (IsAuthenticated,)

    @transaction.atomic
    def post(self, request):
        """تنفيذ إعادة الضبط التشغيلية بعد التحقق من الصلاحية والتأكيد المطلوب."""
        if not request.user.is_superuser or is_platform_owner(request.user):
            raise PermissionDenied("تصفير الحركات متاح لمدير النظام الأعلى فقط.")
        if request.data.get("confirmation") != "تصفير الحركات":
            raise ValidationError({"confirmation": "اكتب عبارة تصفير الحركات كما هي للتأكيد."})
        password = request.data.get("password", "")
        if not isinstance(password, str) or not password or not check_password(password, request.user.password):
            raise PermissionDenied("كلمة مرور مدير النظام غير صحيحة.")
        latest = BackupRun.objects.filter(status=BackupRun.Status.SUCCESS).first()
        if not latest or latest.created_at < timezone.now() - timedelta(hours=24):
            raise ValidationError({"detail": "يلزم إنشاء نسخة احتياطية ناجحة خلال آخر 24 ساعة قبل التصفير."})
        backup_file = Path(latest.backup_file)
        if not latest.backup_file or not backup_file.is_file() or backup_file.stat().st_size < 1024:
            raise ValidationError({"detail": "ملف النسخة الاحتياطية الأخيرة غير موجود أو غير صالح؛ تم إلغاء التصفير."})
        deleted = reset_operational_data()
        AuditEvent.objects.create(
            actor=request.user,
            action=AuditEvent.Action.ACTION,
            resource="core.OperationalReset",
            object_id="all",
            http_method="POST",
            status_code=200,
            succeeded=True,
            metadata={
                "deleted_counts": deleted,
                "backup_run_id": latest.pk,
                "backup_file": latest.backup_file,
            },
        )
        return Response({
            "detail": "تم تصفير الحركات التشغيلية مع الاحتفاظ بالبيانات الأساسية وسجل التدقيق.",
            "deleted_counts": deleted,
            "backup_run_id": latest.pk,
        })
