import json

from django.contrib.auth import authenticate, login, logout
from django.db import DatabaseError, connection
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_protect, ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST, require_http_methods

@require_GET
def health(request):
    payload = {"service": "nexus-api", "status": "ok"}
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        payload["database"] = "ok"
        return JsonResponse(payload)
    except DatabaseError:
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
    from companies.access import is_platform_owner
    return JsonResponse({
        "authenticated": True,
        "user": {
            "id": user.pk,
            "username": user.get_username(),
            "display_name": getattr(user, "display_name", "") or user.get_username(),
            "is_staff": user.is_staff,
            "is_superuser": user.is_superuser,
            "is_platform_owner": is_platform_owner(user),
            "theme_preference": user.theme_preference,
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
    from companies.access import is_platform_owner
    return JsonResponse({
        "authenticated": True,
        "user": {
            "id": user.pk,
            "username": user.get_username(),
            "display_name": getattr(user, "display_name", "") or user.get_username(),
            "is_staff": user.is_staff,
            "is_superuser": user.is_superuser,
            "is_platform_owner": is_platform_owner(user),
            "theme_preference": user.theme_preference,
        },
    })


@csrf_protect
@require_POST
def session_logout(request):
    if not request.user.is_authenticated:
        return JsonResponse({"detail": "لا توجد جلسة دخول نشطة."}, status=401)
    logout(request)
    return JsonResponse({"authenticated": False})


@csrf_protect
@require_http_methods(["PATCH"])
def user_preferences(request):
    """تحديث تفضيلات العرض الخاصة بالمستخدم الحالي دون التأثير على زملائه."""
    if not request.user.is_authenticated:
        return JsonResponse({"detail": "يلزم تسجيل الدخول."}, status=401)
    try:
        payload = json.loads(request.body or b"{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return JsonResponse({"detail": "بيانات التفضيلات غير صالحة."}, status=400)
    theme = payload.get("theme_preference")
    if theme not in {"light", "dark", "system"}:
        return JsonResponse({"detail": "اختر مظهرًا فاتحًا أو داكنًا أو حسب الجهاز."}, status=400)
    request.user.theme_preference = theme
    request.user.save(update_fields=["theme_preference"])
    return JsonResponse({"theme_preference": request.user.theme_preference})
