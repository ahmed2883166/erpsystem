from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse


class PerformanceMetricsAccessTests(TestCase):
    def test_staff_can_read_process_metrics(self):
        user = get_user_model().objects.create_user(username="monitor-admin", password="test-password-123", is_staff=True)
        self.client.force_login(user)
        response = self.client.get(reverse("performance-metrics"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["scope"], "current_application_worker_since_restart")
        self.assertIn("recent", response.json())

    def test_non_staff_cannot_read_process_metrics(self):
        user = get_user_model().objects.create_user(username="monitor-viewer", password="test-password-123")
        self.client.force_login(user)
        response = self.client.get(reverse("performance-metrics"))
        self.assertEqual(response.status_code, 403)
