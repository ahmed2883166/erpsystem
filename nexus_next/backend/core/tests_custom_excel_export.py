from datetime import date, datetime, timezone
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase
from openpyxl import load_workbook
from io import BytesIO
from rest_framework.test import APITestCase

from companies.models import Company, CompanyMembership, Currency
from customers.models import Customer

from core.custom_excel_export import EXPORTS, _excel_value, _field_label, _plain


class CustomExcelExportSchemaTests(SimpleTestCase):
    def test_all_export_allowlists_reference_real_model_fields(self):
        for resource, spec in EXPORTS.items():
            with self.subTest(resource=resource):
                for field_name in spec["fields"]:
                    field = spec["model"]._meta.get_field(field_name)
                    self.assertTrue(field.verbose_name)
                    self.assertTrue(_field_label(spec["model"], field_name))

    def test_export_field_allowlists_are_unique(self):
        for resource, spec in EXPORTS.items():
            with self.subTest(resource=resource):
                self.assertEqual(len(spec["fields"]), len(set(spec["fields"])))

    def test_preview_serializes_decimal_and_dates_without_losing_precision(self):
        self.assertEqual(_plain(Decimal("123.4500")), "123.4500")
        self.assertEqual(_plain(date(2026, 11, 28)), "2026-11-28")
        self.assertEqual(
            _plain(datetime(2026, 11, 28, 12, 30, tzinfo=timezone.utc)),
            "2026-11-28T12:30:00+00:00",
        )

    def test_null_values_are_rendered_as_empty_cells(self):
        self.assertEqual(_plain(None), "")


    def test_spreadsheet_formula_like_text_is_escaped(self):
        for value in ('=HYPERLINK("https://example.invalid")', "+cmd|' /C calc'!A0", "@SUM(A1:A2)"):
            with self.subTest(value=value):
                self.assertTrue(_excel_value(value).startswith("'"))
        self.assertEqual(_excel_value("Ordinary text"), "Ordinary text")
        self.assertEqual(_excel_value(Decimal("-12.5000")), Decimal("-12.5000"))


class CustomExcelExportApiTests(APITestCase):
    def setUp(self):
        currency = Currency.objects.create(code="EGP", name_ar="جنيه مصري")
        self.company = Company.objects.create(
            code="EXPORT", name_ar="شركة التصدير", country="EG",
            activity="commercial", base_currency=currency,
        )
        self.other_company = Company.objects.create(
            code="EXPORT2", name_ar="شركة أخرى", country="EG",
            activity="commercial", base_currency=currency,
        )
        self.user = get_user_model().objects.create_user(
            username="custom-export-accountant", password="test-password",
        )
        CompanyMembership.objects.create(
            user=self.user, company=self.company, role="accountant", all_branches=True,
        )
        self.client.force_authenticate(self.user)
        Customer.objects.create(company=self.company, code="C-001", name_ar="عميل ظاهر")
        Customer.objects.create(company=self.other_company, code="C-002", name_ar="عميل مخفي")

    def test_preview_is_read_only_and_company_scoped(self):
        before = Customer.objects.count()
        response = self.client.get(
            "/api/exports/custom-excel/",
            {"resource": "customers", "company": self.company.pk,
             "fields": "code,name_ar", "preview": "1"},
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["total_rows"], 1)
        self.assertEqual(response.data["headers"], ["كود", "اسم عربي"])
        self.assertEqual(response.data["preview_rows"], [["C-001", "عميل ظاهر"]])
        self.assertEqual(Customer.objects.count(), before)

    def test_download_returns_real_xlsx_with_only_selected_columns(self):
        response = self.client.get(
            "/api/exports/custom-excel/",
            {"resource": "customers", "company": self.company.pk, "fields": "name_ar,code"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response["Content-Type"],
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        workbook = load_workbook(BytesIO(response.content), read_only=True, data_only=True)
        rows = list(workbook.active.iter_rows(values_only=True))
        self.assertEqual(rows[0], ("اسم عربي", "كود"))
        self.assertEqual(rows[1], ("عميل ظاهر", "C-001"))
        self.assertEqual(len(rows), 2)

    def test_unknown_or_unapproved_fields_are_rejected(self):
        response = self.client.get(
            "/api/exports/custom-excel/",
            {"resource": "customers", "company": self.company.pk, "fields": "password"},
        )
        self.assertEqual(response.status_code, 400)

    def test_viewer_cannot_preview_or_download_company_data(self):
        viewer = get_user_model().objects.create_user(
            username="custom-export-viewer", password="test-password",
        )
        CompanyMembership.objects.create(
            user=viewer, company=self.company, role="viewer", all_branches=True,
        )
        self.client.force_authenticate(viewer)
        response = self.client.get(
            "/api/exports/custom-excel/",
            {"resource": "customers", "company": self.company.pk, "mode": "fields"},
        )
        self.assertEqual(response.status_code, 403)

    def test_unassigned_company_data_is_not_exported(self):
        response = self.client.get(
            "/api/exports/custom-excel/",
            {"resource": "customers", "company": self.other_company.pk,
             "fields": "code,name_ar", "preview": "1"},
        )
        self.assertEqual(response.status_code, 403)
