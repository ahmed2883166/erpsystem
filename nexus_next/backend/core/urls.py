from django.urls import path
from .views import csrf_token, health, session_status, session_login, session_logout, user_preferences
from .backup_api import BackupConfigurationView, OperationalResetPreviewView, OperationalResetView
from .performance_api import PerformanceMetricsView
from .custom_excel_export import CustomExcelExportView

urlpatterns = [
    path("health/", health, name="health"),
    path("system/performance/", PerformanceMetricsView.as_view(), name="performance-metrics"),
    path("exports/custom-excel/", CustomExcelExportView.as_view(), name="custom-excel-export"),
    path("csrf/", csrf_token, name="csrf-token"),
    path("auth/session/", session_status, name="session-status"),
    path("auth/login/", session_login, name="session-login"),
    path("auth/logout/", session_logout, name="session-logout"),
    path("auth/preferences/", user_preferences, name="user-preferences"),
    path("backup/configuration/", BackupConfigurationView.as_view(), name="backup-configuration"),
    path("backup/reset-preview/", OperationalResetPreviewView.as_view(), name="backup-reset-preview"),
    path("backup/reset/", OperationalResetView.as_view(), name="backup-reset"),
]
