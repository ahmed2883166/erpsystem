from django.urls import path
from .views import csrf_token, health
from .backup_api import BackupConfigurationView

urlpatterns = [
    path("health/", health, name="health"),
    path("csrf/", csrf_token, name="csrf-token"),
    path("backup/configuration/", BackupConfigurationView.as_view(), name="backup-configuration"),
]
