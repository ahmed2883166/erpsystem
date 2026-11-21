from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase
from .models import Currency

class CompanySetupApiTests(APITestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="reader", password="test-password")
        self.currency = Currency.objects.create(code="SAR", name_ar="ريال سعودي")
    
    def test_setup_endpoints_require_authentication(self):
        response = self.client.get("/api/setup/currencies/")
        self.assertEqual(response.status_code, 403)

    def test_authenticated_user_can_read_currencies(self):
        self.client.force_authenticate(self.user)
        response = self.client.get("/api/setup/currencies/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data[0]["code"], "SAR")

    def test_non_admin_cannot_create_currency(self):
        self.client.force_authenticate(self.user)
        response = self.client.post("/api/setup/currencies/", {
            "code": "AED", "name_ar": "درهم"
        }, format="json")
        self.assertEqual(response.status_code, 403)
