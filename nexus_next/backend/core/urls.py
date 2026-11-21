from django.urls import path
from .views import csrf_token, health

urlpatterns = [
    path("health/", health, name="health"),
    path("csrf/", csrf_token, name="csrf-token"),
]
