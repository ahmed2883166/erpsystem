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
    def test_repeated_failed_logins_lock_the_account(self):
        user = get_user_model().objects.create_user(
            username="locked-session-user", password="correct-test-password",
        )
        client = Client(enforce_csrf_checks=True)
        client.get("/api/csrf/")
        csrf = client.cookies["csrftoken"].value
        for _ in range(5):
            failed = client.post(
                "/api/auth/login/",
                data=json.dumps({"username": user.username, "password": "wrong-password"}),
                content_type="application/json", HTTP_X_CSRFTOKEN=csrf,
            )
            self.assertIn(failed.status_code, (401, 403, 429), failed.content)
        locked = client.post(
            "/api/auth/login/",
            data=json.dumps({"username": user.username, "password": "correct-test-password"}),
            content_type="application/json", HTTP_X_CSRFTOKEN=csrf,
        )
        self.assertIn(locked.status_code, (403, 429), locked.content)

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


class UserThemePreferenceTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="theme-user", password="strong-theme-password",
        )
        self.client.force_login(self.user)

    def test_theme_preference_is_saved_on_the_authenticated_user(self):
        response = self.client.patch(
            "/api/auth/preferences/",
            data=json.dumps({"theme_preference": "dark"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.user.refresh_from_db()
        self.assertEqual(self.user.theme_preference, "dark")
        self.assertEqual(response.json()["theme_preference"], "dark")

    def test_invalid_theme_preference_is_rejected(self):
        response = self.client.patch(
            "/api/auth/preferences/",
            data=json.dumps({"theme_preference": "company-wide-dark"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.user.refresh_from_db()
        self.assertEqual(self.user.theme_preference, "system")

    def test_theme_preference_is_returned_in_the_current_session(self):
        response = self.client.get("/api/auth/session/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["user"]["theme_preference"], "system")
