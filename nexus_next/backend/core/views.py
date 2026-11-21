from django.db import connection
from django.http import JsonResponse
from django.views.decorators.http import require_GET

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
