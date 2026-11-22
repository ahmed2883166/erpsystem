from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from .models import Branch, Company, CompanyMembership, Currency, ExchangeRate
from .services import get_exchange_rate


class ExchangeRateApiTests(APITestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="fx-admin", password="test-password")
        self.egp = Currency.objects.create(code="EGP", name_ar="جنيه مصري")
        self.usd = Currency.objects.create(code="USD", name_ar="دولار أمريكي")
        self.eur = Currency.objects.create(code="EUR", name_ar="يورو", is_active=False)
        self.company = Company.objects.create(
            code="FX-CO", name_ar="شركة اختبار العملات", country="EG",
            activity="commercial", base_currency=self.egp,
        )
        CompanyMembership.objects.create(
            user=self.user, company=self.company, role="admin", all_branches=True,
        )
        self.client.force_authenticate(self.user)

    def test_company_admin_can_create_and_list_historical_exchange_rate(self):
        response = self.client.post("/api/setup/exchange-rates/", {
            "company": self.company.pk, "currency": self.usd.pk,
            "rate_date": "2026-11-20", "rate": "48.2500000000",
            "source": "سعر موثق داخليًا",
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["base_currency_code"], "EGP")
        self.assertEqual(response.data["currency_code"], "USD")
        self.assertEqual(response.data["rate"], "48.2500000000")
        listed = self.client.get(f"/api/setup/exchange-rates/?company={self.company.pk}&currency={self.usd.pk}")
        self.assertEqual(listed.status_code, 200, listed.data)
        self.assertEqual(len(listed.data), 1)

    def test_exchange_rate_rejects_zero_inactive_and_base_currency(self):
        cases = (
            (self.usd, "0"),
            (self.eur, "50"),
            (self.egp, "1"),
        )
        for index, (currency, rate) in enumerate(cases):
            response = self.client.post("/api/setup/exchange-rates/", {
                "company": self.company.pk, "currency": currency.pk,
                "rate_date": f"2026-11-{10 + index:02d}", "rate": rate,
            }, format="json")
            self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(ExchangeRate.objects.count(), 0)

    def test_duplicate_company_currency_date_is_rejected_without_overwriting(self):
        ExchangeRate.objects.create(
            company=self.company, currency=self.usd, rate_date=date(2026, 11, 20),
            rate=Decimal("48.25"),
        )
        response = self.client.post("/api/setup/exchange-rates/", {
            "company": self.company.pk, "currency": self.usd.pk,
            "rate_date": "2026-11-20", "rate": "49.00",
        }, format="json")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(ExchangeRate.objects.get().rate, Decimal("48.2500000000"))

    def test_exchange_rate_history_cannot_be_updated_or_deleted(self):
        record = ExchangeRate.objects.create(
            company=self.company, currency=self.usd, rate_date=date(2026, 11, 20),
            rate=Decimal("48.25"),
        )
        update = self.client.patch(
            f"/api/setup/exchange-rates/{record.pk}/", {"rate": "49"}, format="json"
        )
        delete = self.client.delete(f"/api/setup/exchange-rates/{record.pk}/")
        self.assertEqual(update.status_code, 405)
        self.assertEqual(delete.status_code, 405)
        record.refresh_from_db()
        self.assertEqual(record.rate, Decimal("48.2500000000"))

    def test_rate_lookup_uses_latest_prior_date_and_never_future_rate(self):
        ExchangeRate.objects.create(
            company=self.company, currency=self.usd, rate_date=date(2026, 11, 1),
            rate=Decimal("47.00"),
        )
        ExchangeRate.objects.create(
            company=self.company, currency=self.usd, rate_date=date(2026, 11, 15),
            rate=Decimal("48.00"),
        )
        ExchangeRate.objects.create(
            company=self.company, currency=self.usd, rate_date=date(2026, 12, 1),
            rate=Decimal("50.00"),
        )
        self.assertEqual(
            get_exchange_rate(company=self.company, currency=self.usd, on_date=date(2026, 11, 20)),
            Decimal("48.0000000000"),
        )
        self.assertEqual(
            get_exchange_rate(company=self.company, currency=self.usd, on_date=date(2026, 11, 10)),
            Decimal("47.0000000000"),
        )
        self.assertEqual(
            get_exchange_rate(company=self.company, currency=self.egp, on_date=date(2026, 11, 20)),
            Decimal("1"),
        )

    def test_rate_lookup_requires_rate_on_or_before_transaction_date(self):
        ExchangeRate.objects.create(
            company=self.company, currency=self.usd, rate_date=date(2026, 11, 21),
            rate=Decimal("48.00"),
        )
        with self.assertRaisesMessage(Exception, "لا يوجد سعر صرف"):
            get_exchange_rate(company=self.company, currency=self.usd, on_date=date(2026, 11, 20))
