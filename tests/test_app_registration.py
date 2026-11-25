import unittest
import app as nexus_app


class ApplicationRegistrationTests(unittest.TestCase):
    def test_new_module_routes_are_registered(self):
        endpoints = {rule.endpoint for rule in nexus_app.app.url_map.iter_rules()}
        expected = {
            "tax.tax_report", "tax.classify_document",
            "workforce.employees", "workforce.payroll", "workforce.payroll_run",
            "workforce.post_payroll", "workforce.custody",
            "workforce.custody_expense", "workforce.settle_custody",
            "currency_center.index", "currency_center.create_transaction",
            "currency_center.settle", "currency_center.revalue",
            "party_reports.party_statement",
        }
        self.assertTrue(expected.issubset(endpoints), sorted(expected - endpoints))


if __name__ == "__main__":
    unittest.main()
