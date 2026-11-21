import json

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

class HealthEndpointTests(TestCase):
    def test_health_reports_database_connection(self):
        response = self.client.get(reverse("health"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["service"], "nexus-api")
        self.assertEqual(response.json()["database"], "ok")


class SessionAuthenticationTests(TestCase):
    def test_login_and_logout_require_csrf_and_manage_session(self):
        user = get_user_model().objects.create_user(
            username="session-user", password="strong-test-password",
        )
        client = Client(enforce_csrf_checks=True)
        client.get("/api/csrf/")
        csrf = client.cookies["csrftoken"].value

        missing_csrf = client.post(
            "/api/auth/login/",
            data=json.dumps({"username": user.username, "password": "strong-test-password"}),
            content_type="application/json",
        )
        self.assertEqual(missing_csrf.status_code, 403)

        logged_in = client.post(
            "/api/auth/login/",
            data=json.dumps({"username": user.username, "password": "strong-test-password"}),
            content_type="application/json", HTTP_X_CSRFTOKEN=csrf,
        )
        self.assertEqual(logged_in.status_code, 200, logged_in.json())
        self.assertTrue(logged_in.json()["authenticated"])
        self.assertTrue(client.get("/api/auth/session/").json()["authenticated"])

        csrf = client.cookies["csrftoken"].value
        logged_out = client.post(
            "/api/auth/logout/", data="{}", content_type="application/json",
            HTTP_X_CSRFTOKEN=csrf,
        )
        self.assertEqual(logged_out.status_code, 200, logged_out.json())
        self.assertFalse(client.get("/api/auth/session/").json()["authenticated"])
