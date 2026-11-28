from datetime import date, datetime, timezone
from decimal import Decimal

from django.test import SimpleTestCase

from core.custom_excel_export import EXPORTS, _field_label, _plain


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
