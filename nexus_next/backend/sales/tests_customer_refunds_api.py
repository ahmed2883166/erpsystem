from datetime import date
from decimal import Decimal

from django.db.models import Sum
from rest_framework.test import APITestCase

from accounting.models import AccountRole, ChartOfAccount, JournalEntry, JournalLine
from sales.tests_api import SalesWorkflowTests
from treasury.models import TreasuryAccount, TreasuryTransaction


class CustomerRefundApiTests(APITestCase):
    def setUp(self):
        SalesWorkflowTests.setUp(self)
        cash = ChartOfAccount.objects.get(company=self.company, code="1005")
        self.treasury = TreasuryAccount.objects.create(
            company=self.company, branch=self.branch, account=cash,
            kind=TreasuryAccount.Kind.CASH, code="CASH-CUSTOMER-REFUND",
            name_ar="خزينة مردودات العملاء", currency_code=self.company.base_currency.code,
        )
        created = self.client.post("/api/sales-documents/receipt-batches/", {
            "company": self.company.pk, "customer": self.customer.pk, "branch": self.branch.pk,
            "receipt_number": "REFUND-BATCH-001", "receipt_date": "2026-03-11",
            "method": "cash", "amount": "100.0000", "allocations": [],
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        posted = self.client.post(
            f"/api/sales-documents/receipt-batches/{created.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(posted.status_code, 200, posted.data)
        self.batch_id = created.data["id"]

    def _create_refund(self, number, amount):
        return self.client.post("/api/sales-documents/refunds/", {
            "company": self.company.pk, "customer": self.customer.pk, "branch": self.branch.pk,
            "source_receipt_batch": self.batch_id, "treasury_account": self.treasury.pk,
            "refund_number": number, "refund_date": "2026-03-12", "amount": amount,
        }, format="json")

    def test_refund_api_posts_journal_and_treasury_movement_and_reverses_both(self):
        created = self._create_refund("REFUND-001", "40.0000")
        self.assertEqual(created.status_code, 201, created.data)
        refund_id = created.data["id"]

        posted = self.client.post(
            f"/api/sales-documents/refunds/{refund_id}/post/", {}, format="json",
        )
        self.assertEqual(posted.status_code, 200, posted.data)
        self.assertEqual(posted.data["status"], "posted")
        movement = TreasuryTransaction.objects.get(pk=posted.data["treasury_transaction"])
        self.assertEqual(movement.status, TreasuryTransaction.Status.POSTED)
        self.assertEqual(movement.kind, TreasuryTransaction.Kind.PAYMENT)
        entry = movement.journal_entry
        self.assertEqual(entry.status, JournalEntry.Status.POSTED)
        lines = list(entry.lines.order_by("debit"))
        self.assertEqual(sum((line.debit for line in lines), Decimal("0")), Decimal("40.0000"))
        self.assertEqual(sum((line.credit for line in lines), Decimal("0")), Decimal("40.0000"))

        batch = self.client.get(f"/api/sales-documents/receipt-batches/{self.batch_id}/")
        self.assertEqual(batch.status_code, 200, batch.data)
        self.assertEqual(batch.data["unallocated_amount"], "60.0000")

        blocked_batch_reverse = self.client.post(
            f"/api/sales-documents/receipt-batches/{self.batch_id}/reverse/",
            {"entry_date": "2026-03-13"}, format="json",
        )
        self.assertEqual(blocked_batch_reverse.status_code, 400, blocked_batch_reverse.data)

        reversed_refund = self.client.post(
            f"/api/sales-documents/refunds/{refund_id}/reverse/",
            {"entry_date": "2026-03-14"}, format="json",
        )
        self.assertEqual(reversed_refund.status_code, 200, reversed_refund.data)
        self.assertEqual(reversed_refund.data["status"], "reversed")
        reversal_movement = TreasuryTransaction.objects.get(
            pk=reversed_refund.data["reversal_treasury_transaction"],
        )
        self.assertEqual(reversal_movement.kind, TreasuryTransaction.Kind.RECEIPT)
        self.assertEqual(reversal_movement.status, TreasuryTransaction.Status.POSTED)
        self.assertEqual(reversal_movement.journal_entry.status, JournalEntry.Status.POSTED)

        batch = self.client.get(f"/api/sales-documents/receipt-batches/{self.batch_id}/")
        self.assertEqual(batch.data["unallocated_amount"], "100.0000")
        reversed_batch = self.client.post(
            f"/api/sales-documents/receipt-batches/{self.batch_id}/reverse/",
            {"entry_date": "2026-03-15"}, format="json",
        )
        self.assertEqual(reversed_batch.status_code, 200, reversed_batch.data)

    def test_refund_cannot_exceed_unallocated_advance_and_posted_refund_cannot_be_reused(self):
        too_large = self._create_refund("REFUND-TOO-LARGE", "100.0001")
        self.assertEqual(too_large.status_code, 201, too_large.data)
        rejected = self.client.post(
            f"/api/sales-documents/refunds/{too_large.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(rejected.status_code, 400, rejected.data)
        self.assertIn("المتاحة", str(rejected.data))

        valid = self._create_refund("REFUND-VALID", "70.0000")
        self.assertEqual(valid.status_code, 201, valid.data)
        posted = self.client.post(
            f"/api/sales-documents/refunds/{valid.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(posted.status_code, 200, posted.data)

        another = self._create_refund("REFUND-OVERLAP", "31.0000")
        self.assertEqual(another.status_code, 201, another.data)
        rejected_again = self.client.post(
            f"/api/sales-documents/refunds/{another.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(rejected_again.status_code, 400, rejected_again.data)
        batch = self.client.get(f"/api/sales-documents/receipt-batches/{self.batch_id}/")
        self.assertEqual(batch.data["unallocated_amount"], "30.0000")



class CustomerLifecycleReconciliationTests(APITestCase):
    """Exercise the full customer AR lifecycle through public APIs and GL."""

    def setUp(self):
        SalesWorkflowTests.setUp(self)
        bank_gl = ChartOfAccount.objects.get(company=self.company, code="1100")
        self.bank_treasury = TreasuryAccount.objects.create(
            company=self.company, branch=self.branch, account=bank_gl,
            kind=TreasuryAccount.Kind.BANK, code="BANK-CUSTOMER-LIFECYCLE",
            name_ar="بنك دورة العملاء", currency_code=self.company.base_currency.code,
        )

    def _create_delivery(self, *args, **kwargs):
        return SalesWorkflowTests._create_delivery(self, *args, **kwargs)

    def test_invoice_partial_receipt_batch_advance_refund_reversal_reconciles(self):
        delivery_id, delivery_line_id = self._create_delivery("DEL-CUSTOMER-LIFECYCLE")
        invoice_response = self.client.post("/api/sales-documents/invoices/", {
            "company": self.company.pk, "customer": self.customer.pk,
            "delivery": delivery_id, "invoice_number": "INV-CUSTOMER-LIFECYCLE",
            "invoice_date": "2026-03-10",
            "lines": [{
                "delivery_line": delivery_line_id, "unit_price": "100", "tax_rate": "0",
            }],
        }, format="json")
        self.assertEqual(invoice_response.status_code, 201, invoice_response.data)
        invoice_id = invoice_response.data["id"]
        invoice_posted = self.client.post(
            f"/api/sales-documents/invoices/{invoice_id}/post/", {}, format="json",
        )
        self.assertEqual(invoice_posted.status_code, 200, invoice_posted.data)
        self.assertEqual(invoice_posted.data["grand_total"], "200.0000")

        partial_receipt = self.client.post("/api/sales-documents/receipts/", {
            "company": self.company.pk, "customer": self.customer.pk,
            "invoice": invoice_id, "receipt_number": "RC-CUSTOMER-LIFECYCLE",
            "receipt_date": "2026-03-11", "method": "bank",
            "payment_account": self.bank_treasury.account_id, "amount": "20",
        }, format="json")
        self.assertEqual(partial_receipt.status_code, 201, partial_receipt.data)
        partial_posted = self.client.post(
            f"/api/sales-documents/receipts/{partial_receipt.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(partial_posted.status_code, 200, partial_posted.data)

        batch_response = self.client.post("/api/sales-documents/receipt-batches/", {
            "company": self.company.pk, "customer": self.customer.pk, "branch": self.branch.pk,
            "receipt_number": "BATCH-CUSTOMER-LIFECYCLE", "receipt_date": "2026-03-12",
            "method": "bank", "payment_account": self.bank_treasury.account_id,
            "amount": "100", "allocations": [],
        }, format="json")
        self.assertEqual(batch_response.status_code, 201, batch_response.data)
        batch_id = batch_response.data["id"]
        batch_posted = self.client.post(
            f"/api/sales-documents/receipt-batches/{batch_id}/post/", {}, format="json",
        )
        self.assertEqual(batch_posted.status_code, 200, batch_posted.data)

        first_allocation = self.client.post(
            f"/api/sales-documents/receipt-batches/{batch_id}/allocate/",
            {"invoice": invoice_id, "amount": "80", "allocation_date": "2026-03-13"},
            format="json",
        )
        self.assertEqual(first_allocation.status_code, 200, first_allocation.data)
        self.assertEqual(first_allocation.data["unallocated_amount"], "20.0000")

        refund = self.client.post("/api/sales-documents/refunds/", {
            "company": self.company.pk, "customer": self.customer.pk, "branch": self.branch.pk,
            "source_receipt_batch": batch_id, "treasury_account": self.bank_treasury.pk,
            "refund_number": "REF-CUSTOMER-LIFECYCLE", "refund_date": "2026-03-14",
            "amount": "10",
        }, format="json")
        self.assertEqual(refund.status_code, 201, refund.data)
        refund_posted = self.client.post(
            f"/api/sales-documents/refunds/{refund.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(refund_posted.status_code, 200, refund_posted.data)
        self.assertEqual(refund_posted.data["status"], "posted")

        refund_reversed = self.client.post(
            f"/api/sales-documents/refunds/{refund.data['id']}/reverse/",
            {"entry_date": "2026-03-15"}, format="json",
        )
        self.assertEqual(refund_reversed.status_code, 200, refund_reversed.data)
        self.assertEqual(refund_reversed.data["status"], "reversed")

        final_allocation = self.client.post(
            f"/api/sales-documents/receipt-batches/{batch_id}/allocate/",
            {"invoice": invoice_id, "amount": "20", "allocation_date": "2026-03-16"},
            format="json",
        )
        self.assertEqual(final_allocation.status_code, 200, final_allocation.data)
        self.assertEqual(final_allocation.data["unallocated_amount"], "0.0000")

        invoice = self.client.get(f"/api/sales-documents/invoices/{invoice_id}/")
        self.assertEqual(invoice.status_code, 200, invoice.data)
        self.assertEqual(invoice.data["balance_due"], "80.0000")

        statement = self.client.get(
            f"/api/sales-documents/invoices/customer-statement/?company={self.company.pk}"
            f"&customer={self.customer.pk}&date_from=2026-03-10&date_to=2026-03-16"
        )
        self.assertEqual(statement.status_code, 200, statement.data)
        self.assertEqual(statement.data["closing_balance"], "80.0000")
        event_types = [row["document_type"] for row in statement.data["rows"]]
        self.assertIn("receipt", event_types)
        self.assertIn("receipt_batch", event_types)
        self.assertIn("customer_refund", event_types)
        self.assertIn("customer_refund_reversal", event_types)

        aging = self.client.get(
            f"/api/sales-documents/invoices/customer-aging/?company={self.company.pk}&as_of=2026-03-16"
        )
        self.assertEqual(aging.status_code, 200, aging.data)
        self.assertEqual(aging.data["totals"]["total_due"], "80.0000")
        self.assertEqual(Decimal(aging.data["totals"]["unallocated_advances"]), Decimal("0"))

        reconciliation = self.client.get(
            f"/api/accounting/subledger-reconciliation/?company={self.company.pk}&as_of=2026-03-16"
        )
        self.assertEqual(reconciliation.status_code, 200, reconciliation.data)
        customer_row = next(row for row in reconciliation.data["rows"] if row["key"] == "customers")
        self.assertEqual(customer_row["status"], "matched", customer_row)
        self.assertEqual(Decimal(customer_row["subledger_balance"]), Decimal("80"))
        self.assertEqual(Decimal(customer_row["gl_balance"]), Decimal("80"))
        self.assertEqual(Decimal(customer_row["difference"]), Decimal("0"))

        receivable_account = ChartOfAccount.objects.get(
            company=self.company, role_assignments__role=AccountRole.Role.ACCOUNTS_RECEIVABLE,
        )
        bank_account = self.bank_treasury.account
        for account, expected_balance in (
            (receivable_account, Decimal("80")),
            (bank_account, Decimal("120")),
        ):
            totals = JournalLine.objects.filter(
                account=account, entry__company=self.company,
                entry__branch=self.branch, entry__entry_date__lte=date(2026, 3, 16),
                entry__status__in=(JournalEntry.Status.POSTED, JournalEntry.Status.REVERSED),
            ).aggregate(debit_total=Sum("debit"), credit_total=Sum("credit"))
            self.assertEqual(
                (totals["debit_total"] or Decimal("0")) - (totals["credit_total"] or Decimal("0")),
                expected_balance,
                account.code,
            )

        original_refund_movement = TreasuryTransaction.objects.get(
            pk=refund_posted.data["treasury_transaction"],
        )
        reversal_movement = TreasuryTransaction.objects.get(
            pk=refund_reversed.data["reversal_treasury_transaction"],
        )
        self.assertEqual(original_refund_movement.status, TreasuryTransaction.Status.REVERSED)
        self.assertEqual(reversal_movement.status, TreasuryTransaction.Status.POSTED)
        self.assertEqual(reversal_movement.kind, TreasuryTransaction.Kind.RECEIPT)

        sales_entries = JournalEntry.objects.filter(
            company=self.company, branch=self.branch, source_type=JournalEntry.Source.SALES,
            entry_date__lte=date(2026, 3, 16),
            status__in=(JournalEntry.Status.POSTED, JournalEntry.Status.REVERSED),
        )
        for entry in sales_entries:
            totals = entry.lines.aggregate(debit_total=Sum("debit"), credit_total=Sum("credit"))
            self.assertEqual(totals["debit_total"], totals["credit_total"], entry.entry_number)
