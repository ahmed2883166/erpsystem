from decimal import Decimal

from rest_framework.test import APITestCase

from accounting.models import ChartOfAccount, JournalEntry
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
