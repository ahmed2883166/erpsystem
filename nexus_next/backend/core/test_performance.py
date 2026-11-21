from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse


class PerformanceMetricsAccessTests(TestCase):
    def test_staff_can_read_process_and_host_metrics(self):
        user = get_user_model().objects.create_user(
            username="monitor-admin", password="test-password-123", is_staff=True
        )
        self.client.force_login(user)
        response = self.client.get(reverse("performance-metrics"))
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["scope"], "current_application_worker_since_restart")
        self.assertIn("recent", payload)
        self.assertIn("system", payload)
        self.assertIn("postgresql", payload)
        self.assertIn("available", payload["system"])
        self.assertIn("available", payload["postgresql"])

    def test_non_staff_cannot_read_process_metrics(self):
        user = get_user_model().objects.create_user(
            username="monitor-viewer", password="test-password-123"
        )
        self.client.force_login(user)
        response = self.client.get(reverse("performance-metrics"))
        self.assertEqual(response.status_code, 403)
