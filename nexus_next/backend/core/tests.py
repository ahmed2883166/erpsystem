from django.test import TestCase
from django.urls import reverse

class HealthEndpointTests(TestCase):
    def test_health_reports_database_connection(self):
        response = self.client.get(reverse("health"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["service"], "nexus-api")
        self.assertEqual(response.json()["database"], "ok")
