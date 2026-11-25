from decimal import Decimal

from rest_framework.test import APITestCase

from accounting.models import AccountRole, JournalEntry, JournalLine
from treasury.models import TreasuryAccount, TreasuryTransaction


class CustomerRefundTests(APITestCase):
    def setUp(self):
        from sales.tests_api import SalesWorkflowTests
        SalesWorkflowTests.setUp(self)
        self.cash_account = AccountRole.objects.get(
            company=self.company, role=AccountRole.Role.CASH,
        ).account
        self.treasury = TreasuryAccount.objects.create(
            company=self.company, branch=self.branch, account=self.cash_account,
            kind=TreasuryAccount.Kind.CASH, code="CASH-REFUND",
            name_ar="خزينة رد العملاء", opening_balance=Decimal("1000"),
        )

    def _create_delivery(self, *args, **kwargs):
        from sales.tests_api import SalesWorkflowTests
        return SalesWorkflowTests._create_delivery(self, *args, **kwargs)

    def _create_posted_invoice(self, number):
        delivery_id, delivery_line_id = self._create_delivery(
            number.replace("INV", "DEL"),
        )
        created = self.client.post("/api/sales-documents/invoices/", {
            "company": self.company.pk,
            "customer": self.customer.pk,
            "delivery": delivery_id,
            "invoice_number": number,
            "invoice_date": "2026-03-10",
            "lines": [{
                "delivery_line": delivery_line_id,
                "unit_price": "100",
                "tax_rate": "0",
            }],
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        posted = self.client.post(
            f"/api/sales-documents/invoices/{created.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(posted.status_code, 200, posted.data)
        return created.data

    def _post_invoice_receipt(self, invoice, number):
        created = self.client.post("/api/sales-documents/receipts/", {
            "company": self.company.pk,
            "customer": self.customer.pk,
            "invoice": invoice["id"],
            "receipt_number": number,
            "receipt_date": "2026-03-11",
            "method": "cash",
            "payment_account": self.cash_account.pk,
            "amount": invoice["grand_total"],
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        posted = self.client.post(
            f"/api/sales-documents/receipts/{created.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(posted.status_code, 200, posted.data)

    def _create_posted_return(self, invoice, number):
        self._post_invoice_receipt(invoice, f"{number}-RECEIPT")
        created = self.client.post("/api/sales-documents/returns/", {
            "company": self.company.pk,
            "invoice": invoice["id"],
            "return_number": number,
            "return_date": "2026-03-12",
            "lines": [{
                "invoice_line": invoice["invoice_lines"][0]["id"],
                "quantity": "1",
            }],
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        posted = self.client.post(
            f"/api/sales-documents/returns/{created.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(posted.status_code, 200, posted.data)
        return posted.data

    def _create_posted_advance(self, number, amount):
        created = self.client.post("/api/sales-documents/receipt-batches/", {
            "company": self.company.pk,
            "customer": self.customer.pk,
            "branch": self.branch.pk,
            "receipt_number": number,
            "receipt_date": "2026-03-11",
            "method": "cash",
            "payment_account": self.cash_account.pk,
            "amount": str(amount),
            "allocations": [],
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        posted = self.client.post(
            f"/api/sales-documents/receipt-batches/{created.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(posted.status_code, 200, posted.data)
        return posted.data

    def _create_refund(self, number, *, amount, source_return=None, source_batch=None, date="2026-03-13"):
        return self.client.post("/api/sales-documents/refunds/", {
            "company": self.company.pk,
            "customer": self.customer.pk,
            "refund_number": number,
            "refund_date": date,
            "source_return": source_return,
            "source_receipt_batch": source_batch,
            "treasury_account": self.treasury.pk,
            "amount": str(amount),
        }, format="json")

    def _assert_balanced(self, journal_entry_id):
        lines = JournalLine.objects.filter(entry_id=journal_entry_id)
        debit = sum(lines.values_list("debit", flat=True), Decimal("0"))
        credit = sum(lines.values_list("credit", flat=True), Decimal("0"))
        self.assertEqual(debit, credit)

    def test_partial_return_refund_posts_cash_and_blocks_return_reversal_until_refund_reversed(self):
        invoice = self._create_posted_invoice("REFUND-INV-RETURN")
        sales_return = self._create_posted_return(invoice, "REFUND-RET-001")
        self.assertEqual(sales_return["grand_total"], "100.0000")

        created = self._create_refund(
            "REFUND-RET-CASH-001", amount="40", source_return=sales_return["id"],
        )
        self.assertEqual(created.status_code, 201, created.data)
        posted = self.client.post(
            f"/api/sales-documents/refunds/{created.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(posted.status_code, 200, posted.data)
        self.assertEqual(posted.data["status"], "posted")
        self.assertEqual(posted.data["amount"], "40.0000")
        self.assertIsNotNone(posted.data["treasury_transaction"])
        treasury_doc = TreasuryTransaction.objects.get(pk=posted.data["treasury_transaction"])
        self._assert_balanced(treasury_doc.journal_entry_id)

        blocked = self.client.post(
            f"/api/sales-documents/returns/{sales_return['id']}/reverse/",
            {"entry_date": "2026-03-14"}, format="json",
        )
        self.assertEqual(blocked.status_code, 400, blocked.data)

        statement = self.client.get(
            f"/api/sales-documents/invoices/customer-statement/?company={self.company.pk}"
            f"&customer={self.customer.pk}&date_from=2026-03-10&date_to=2026-03-13"
        )
        self.assertEqual(statement.status_code, 200, statement.data)
        refund_rows = [row for row in statement.data["rows"] if row["document_type"] == "customer_refund"]
        self.assertEqual(len(refund_rows), 1)
        self.assertEqual(refund_rows[0]["debit"], "40.0000")

        reversed_refund = self.client.post(
            f"/api/sales-documents/refunds/{created.data['id']}/reverse/",
            {"entry_date": "2026-03-14"}, format="json",
        )
        self.assertEqual(reversed_refund.status_code, 200, reversed_refund.data)
        self.assertEqual(reversed_refund.data["status"], "reversed")
        self.assertIsNotNone(reversed_refund.data["reversal_journal_entry"])
        self._assert_balanced(reversed_refund.data["reversal_journal_entry"])

        reversed_return = self.client.post(
            f"/api/sales-documents/returns/{sales_return['id']}/reverse/",
            {"entry_date": "2026-03-15"}, format="json",
        )
        self.assertEqual(reversed_return.status_code, 200, reversed_return.data)

    def test_partial_advance_refund_reduces_unallocated_advance_and_can_be_reversed(self):
        batch = self._create_posted_advance("REFUND-ADV-001", "50")
        created = self._create_refund(
            "REFUND-ADV-CASH-001", amount="20", source_batch=batch["id"],
        )
        self.assertEqual(created.status_code, 201, created.data)
        posted = self.client.post(
            f"/api/sales-documents/refunds/{created.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(posted.status_code, 200, posted.data)
        self.assertEqual(posted.data["status"], "posted")

        aging = self.client.get(
            f"/api/sales-documents/invoices/customer-aging/?company={self.company.pk}&as_of=2026-03-13"
        )
        self.assertEqual(aging.status_code, 200, aging.data)
        self.assertEqual(aging.data["totals"]["unallocated_advances"], "30.0000")

        over_allocation = self.client.post(
            f"/api/sales-documents/receipt-batches/{batch['id']}/allocate/",
            {"invoice": self._create_posted_invoice("REFUND-ADV-INV")["id"], "amount": "31",
             "allocation_date": "2026-03-14"},
            format="json",
        )
        self.assertEqual(over_allocation.status_code, 400, over_allocation.data)

        statement = self.client.get(
            f"/api/sales-documents/invoices/customer-statement/?company={self.company.pk}"
            f"&customer={self.customer.pk}&date_from=2026-03-11&date_to=2026-03-13"
        )
        self.assertEqual(statement.status_code, 200, statement.data)
        refund_rows = [row for row in statement.data["rows"] if row["document_type"] == "customer_refund"]
        self.assertEqual(len(refund_rows), 1)
        self.assertEqual(refund_rows[0]["debit"], "20.0000")

        reversed_refund = self.client.post(
            f"/api/sales-documents/refunds/{created.data['id']}/reverse/",
            {"entry_date": "2026-03-14"}, format="json",
        )
        self.assertEqual(reversed_refund.status_code, 200, reversed_refund.data)
        self.assertEqual(reversed_refund.data["status"], "reversed")

        aging_after_reversal = self.client.get(
            f"/api/sales-documents/invoices/customer-aging/?company={self.company.pk}&as_of=2026-03-14"
        )
        self.assertEqual(aging_after_reversal.status_code, 200, aging_after_reversal.data)
        self.assertEqual(aging_after_reversal.data["totals"]["unallocated_advances"], "50.0000")

        reversed_batch = self.client.post(
            f"/api/sales-documents/receipt-batches/{batch['id']}/reverse/",
            {"entry_date": "2026-03-15"}, format="json",
        )
        self.assertEqual(reversed_batch.status_code, 200, reversed_batch.data)

    def test_refund_cannot_exceed_return_credit_or_unallocated_advance(self):
        invoice = self._create_posted_invoice("REFUND-INV-LIMIT")
        sales_return = self._create_posted_return(invoice, "REFUND-RET-LIMIT")
        too_much_return = self._create_refund(
            "REFUND-RET-OVER", amount="100.0001", source_return=sales_return["id"],
        )
        self.assertEqual(too_much_return.status_code, 201, too_much_return.data)
        rejected_return = self.client.post(
            f"/api/sales-documents/refunds/{too_much_return.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(rejected_return.status_code, 400, rejected_return.data)

        batch = self._create_posted_advance("REFUND-ADV-LIMIT", "25")
        too_much_advance = self._create_refund(
            "REFUND-ADV-OVER", amount="25.0001", source_batch=batch["id"],
        )
        self.assertEqual(too_much_advance.status_code, 201, too_much_advance.data)
        rejected_advance = self.client.post(
            f"/api/sales-documents/refunds/{too_much_advance.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(rejected_advance.status_code, 400, rejected_advance.data)
