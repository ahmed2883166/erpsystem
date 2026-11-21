import json

from django.contrib.auth import authenticate, login, logout
from django.db import connection
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_protect, ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

@require_GET
def health(request):
    payload = {"service": "nexus-api", "status": "ok"}
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        payload["database"] = "ok"
        return JsonResponse(payload)
    except Exception:
        return JsonResponse(
            {"service": "nexus-api", "status": "degraded", "database": "unavailable"},
            status=503,
        )


@ensure_csrf_cookie
def csrf_token(request):
    return JsonResponse({"detail": "CSRF cookie initialized."})


@require_GET
def session_status(request):
    user = request.user
    if not user.is_authenticated:
        return JsonResponse({"authenticated": False})
    return JsonResponse({
        "authenticated": True,
        "user": {
            "id": user.pk,
            "username": user.get_username(),
            "display_name": getattr(user, "display_name", "") or user.get_username(),
            "is_staff": user.is_staff,
            "is_superuser": user.is_superuser,
        },
    })


@csrf_protect
@require_POST
def session_login(request):
    try:
        payload = json.loads(request.body or b"{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return JsonResponse({"detail": "بيانات تسجيل الدخول غير صالحة."}, status=400)
    username = str(payload.get("username", "")).strip()
    password = payload.get("password", "")
    if not username or not isinstance(password, str) or not password:
        return JsonResponse({"detail": "أدخل اسم المستخدم وكلمة المرور."}, status=400)
    user = authenticate(request, username=username, password=password)
    if not user or not user.is_active:
        return JsonResponse({"detail": "اسم المستخدم أو كلمة المرور غير صحيحة."}, status=401)
    login(request, user)
    return JsonResponse({
        "authenticated": True,
        "user": {
            "id": user.pk,
            "username": user.get_username(),
            "display_name": getattr(user, "display_name", "") or user.get_username(),
            "is_staff": user.is_staff,
            "is_superuser": user.is_superuser,
        },
    })


@csrf_protect
@require_POST
def session_logout(request):
    if not request.user.is_authenticated:
        return JsonResponse({"detail": "لا توجد جلسة دخول نشطة."}, status=401)
    logout(request)
    return JsonResponse({"authenticated": False})
