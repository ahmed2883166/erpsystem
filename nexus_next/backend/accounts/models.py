from django.contrib.auth.models import AbstractUser
from django.db import models

class User(AbstractUser):
    """NEXUS user; authorization is managed through Django groups and permissions."""
    display_name = models.CharField(max_length=160, blank=True)
    preferred_language = models.CharField(max_length=8, default="ar")

    class Meta:
        verbose_name = "مستخدم"
        verbose_name_plural = "المستخدمون"

    def __str__(self):
        return self.display_name or self.username
