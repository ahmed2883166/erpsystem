from rest_framework.test import APITestCase


class SalesPerformanceHistoricalReturnTests(APITestCase):
    def setUp(self):
        from sales.tests_api import SalesWorkflowTests
        SalesWorkflowTests.setUp(self)

    def _create_delivery(self, *args, **kwargs):
        from sales.tests_api import SalesWorkflowTests
        return SalesWorkflowTests._create_delivery(self, *args, **kwargs)

    def test_return_reversed_after_period_end_remains_in_historical_sales_performance(self):
        delivery_id, delivery_line_id = self._create_delivery("PERF-HIST-DEL-001")
        invoice = self.client.post("/api/sales-documents/invoices/", {
            "company": self.company.pk,
            "customer": self.customer.pk,
            "delivery": delivery_id,
            "invoice_number": "PERF-HIST-INV-001",
            "invoice_date": "2026-03-10",
            "lines": [{
                "delivery_line": delivery_line_id,
                "unit_price": "100",
                "tax_rate": "0",
            }],
        }, format="json")
        self.assertEqual(invoice.status_code, 201, invoice.data)
        posted_invoice = self.client.post(
            f"/api/sales-documents/invoices/{invoice.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(posted_invoice.status_code, 200, posted_invoice.data)

        sales_return = self.client.post("/api/sales-documents/returns/", {
            "company": self.company.pk,
            "invoice": invoice.data["id"],
            "return_number": "PERF-HIST-RET-001",
            "return_date": "2026-03-12",
            "lines": [{
                "invoice_line": invoice.data["invoice_lines"][0]["id"],
                "quantity": "1",
            }],
        }, format="json")
        self.assertEqual(sales_return.status_code, 201, sales_return.data)
        posted_return = self.client.post(
            f"/api/sales-documents/returns/{sales_return.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(posted_return.status_code, 200, posted_return.data)

        reversed_return = self.client.post(
            f"/api/sales-documents/returns/{sales_return.data['id']}/reverse/",
            {"entry_date": "2026-03-14"}, format="json",
        )
        self.assertEqual(reversed_return.status_code, 200, reversed_return.data)

        historical = self.client.get(
            f"/api/sales-documents/invoices/sales-performance/?company={self.company.pk}"
            "&date_from=2026-03-10&date_to=2026-03-13"
        )
        self.assertEqual(historical.status_code, 200, historical.data)
        self.assertEqual(historical.data["totals"]["return_count"], 1)
        self.assertEqual(historical.data["totals"]["return_total"], "100.0000")
        self.assertEqual(historical.data["totals"]["net_sales"], "0.0000")

        through_reversal = self.client.get(
            f"/api/sales-documents/invoices/sales-performance/?company={self.company.pk}"
            "&date_from=2026-03-10&date_to=2026-03-14"
        )
        self.assertEqual(through_reversal.status_code, 200, through_reversal.data)
        self.assertEqual(through_reversal.data["totals"]["return_count"], 0)
        self.assertEqual(through_reversal.data["totals"]["return_total"], "0.0000")
        self.assertEqual(through_reversal.data["totals"]["net_sales"], "100.0000")
