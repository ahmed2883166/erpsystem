import unittest

from app import app


class NewWorkflowTemplateTests(unittest.TestCase):
    def test_stock_and_cash_bank_workflow_templates_compile(self):
        templates = (
            "stock_reconciliation.html",
            "stock_pending_movements.html",
            "stock_warehouses.html",
            "cash_accounts.html",
            "bank_reconciliation.html",
            "bank.html",
        )
        for template_name in templates:
            with self.subTest(template=template_name):
                template = app.jinja_env.get_template(template_name)
                self.assertIsNotNone(template)


if __name__ == "__main__":
    unittest.main()
