from django.urls import path
from .api import AuditEventListView

urlpatterns = [
    path("events/", AuditEventListView.as_view(), name="audit-events"),
]
