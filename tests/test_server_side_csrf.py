import unittest

from flask import Response

from app import app, add_server_side_csrf_fields


class ServerSideCsrfFormTests(unittest.TestCase):
    def test_post_forms_receive_csrf_without_javascript(self):
        with app.test_request_context("/csrf-test"):
            response = Response(
                '<form method="post" action="/save"><input name="value"></form>',
                mimetype="text/html",
            )
            updated = add_server_side_csrf_fields(response)
            html = updated.get_data(as_text=True)
            self.assertIn('name="csrf_token"', html)
            self.assertEqual(html.count('name="csrf_token"'), 1)

    def test_existing_manual_csrf_field_is_not_duplicated(self):
        with app.test_request_context("/csrf-test"):
            response = Response(
                '<form method="post"><input type="hidden" name="csrf_token" value="existing"></form>',
                mimetype="text/html",
            )
            updated = add_server_side_csrf_fields(response)
            self.assertEqual(updated.get_data(as_text=True).count('name="csrf_token"'), 1)


if __name__ == "__main__":
    unittest.main()
