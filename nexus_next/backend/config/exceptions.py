from django.db.models.deletion import ProtectedError
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import exception_handler


def nexus_exception_handler(exc, context):
    """توحيد صياغة استجابات الأخطاء مع الحفاظ على رموز HTTP وتفاصيل التحقق."""
    if isinstance(exc, ProtectedError):
        protected = getattr(exc, "protected_objects", ())
        labels = sorted({obj._meta.verbose_name for obj in protected})
        detail = "لا يمكن حذف السجل لوجود بيانات مرتبطة به"
        if labels:
            detail += ": " + "، ".join(labels)
        detail += ". أوقف السجل بدلًا من حذفه أو عالج الارتباطات أولًا."
        return Response({"detail": detail}, status=status.HTTP_400_BAD_REQUEST)
    return exception_handler(exc, context)
