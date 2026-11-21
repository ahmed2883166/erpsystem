from collections import deque
from statistics import median
from threading import Lock
from time import perf_counter

import psutil
from django.db import connection


_EVENTS = deque(maxlen=250)
_LOCK = Lock()
_SLOW_REQUEST_MS = 1000.0


def _record(event):
    """تسجيل عينة أداء للطلب داخل ذاكرة العامل الحالي."""
    with _LOCK:
        _EVENTS.append(event)


def _system_resources():
    """قراءة مؤشرات استهلاك المعالج والذاكرة المتاحة."""
    try:
        memory = psutil.virtual_memory()
        return {
            "available": True,
            "cpu_percent": psutil.cpu_percent(interval=None),
            "memory_percent": round(memory.percent, 2),
            "memory_total_mb": round(memory.total / (1024 * 1024), 1),
            "memory_available_mb": round(memory.available / (1024 * 1024), 1),
        }
    except (OSError, RuntimeError):
        return {"available": False}


def _postgres_resources():
    """قراءة مؤشرات اتصالات PostgreSQL وحجم قاعدة البيانات."""
    if connection.vendor != "postgresql":
        return {"available": False, "reason": "postgresql_only"}
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    COUNT(*) AS total_connections,
                    COUNT(*) FILTER (WHERE state = 'active') AS active_connections,
                    COUNT(*) FILTER (WHERE state = 'idle') AS idle_connections
                FROM pg_stat_activity
                WHERE datname = current_database()
                """
            )
            total, active, idle = cursor.fetchone()
            cursor.execute("SELECT current_setting('max_connections')::integer, pg_database_size(current_database())")
            maximum, database_bytes = cursor.fetchone()
        return {
            "available": True,
            "database_connections": total,
            "active_connections": active,
            "idle_connections": idle,
            "max_connections": maximum,
            "database_size_mb": round(database_bytes / (1024 * 1024), 2),
            "connection_usage_percent": round(total * 100 / maximum, 2) if maximum else 0,
        }
    except Exception:
        # Monitoring must never make normal application requests fail.
        return {"available": False, "reason": "database_metrics_unavailable"}


def performance_snapshot():
    """تجميع ملخص مؤشرات الأداء والطلبات المسجلة."""
    with _LOCK:
        events = list(_EVENTS)
    durations = sorted(item["duration_ms"] for item in events)
    count = len(events)
    errors = sum(1 for item in events if item["status_code"] >= 500)
    slow = sum(1 for item in events if item["duration_ms"] >= _SLOW_REQUEST_MS)
    query_total = sum(item["db_queries"] for item in events)
    db_total = sum(item["db_duration_ms"] for item in events)
    p95 = durations[min(len(durations) - 1, int(len(durations) * .95))] if durations else 0
    return {
        "scope": "current_application_worker_since_restart",
        "sample_size": count,
        "average_duration_ms": round(sum(durations) / count, 2) if count else 0,
        "median_duration_ms": round(median(durations), 2) if count else 0,
        "p95_duration_ms": round(p95, 2),
        "error_count": errors,
        "slow_request_count": slow,
        "average_db_queries": round(query_total / count, 2) if count else 0,
        "total_db_duration_ms": round(db_total, 2),
        "system": _system_resources(),
        "postgresql": _postgres_resources(),
        "recent": list(reversed(events[-30:])),
    }


class PerformanceMetricsMiddleware:
    """Low-overhead, in-memory request and SQL timing; no SQL text or payloads are retained."""

    def __init__(self, get_response):
        """تهيئة وسيط قياس الأداء وربطه بمعالج الطلب التالي."""
        self.get_response = get_response

    def __call__(self, request):
        """قياس زمن الطلب وعدد استعلامات قاعدة البيانات وتسجيل النتائج."""
        started = perf_counter()
        query_count = 0
        query_duration = 0.0
        status_code = 500

        def track_execute(execute, sql, params, many, context):
            """قياس زمن تنفيذ استعلام قاعدة البيانات دون تسجيل نصه أو بياناته الحساسة."""
            nonlocal query_count, query_duration
            query_count += 1
            query_started = perf_counter()
            try:
                return execute(sql, params, many, context)
            finally:
                query_duration += (perf_counter() - query_started) * 1000

        try:
            with connection.execute_wrapper(track_execute):
                response = self.get_response(request)
            status_code = response.status_code
            return response
        finally:
            route = getattr(getattr(request, "resolver_match", None), "route", None)
            safe_route = route or request.path_info.split("?")[0][:160]
            _record({
                "route": safe_route,
                "method": request.method,
                "status_code": status_code,
                "duration_ms": round((perf_counter() - started) * 1000, 2),
                "db_queries": query_count,
                "db_duration_ms": round(query_duration, 2),
            })
