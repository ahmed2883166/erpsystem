from datetime import date
from decimal import Decimal

from rest_framework.test import APITestCase

from accounting.models import JournalEntry, JournalLine
from accounting.services import post_journal, reverse_operational_journal


class CustomerReceiptBatchTests(APITestCase):
    def setUp(self):
        from sales.tests_api import SalesWorkflowTests
        SalesWorkflowTests.setUp(self)

    def _create_delivery(self, *args, **kwargs):
        from sales.tests_api import SalesWorkflowTests
        return SalesWorkflowTests._create_delivery(self, *args, **kwargs)

    def _create_posted_invoice(self, number):
        delivery_id, delivery_line_id = self._create_delivery(
            number.replace("INV", "DEL"),
        )
        response = self.client.post("/api/sales-documents/invoices/", {
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
        self.assertEqual(response.status_code, 201, response.data)
        posted = self.client.post(
            f"/api/sales-documents/invoices/{response.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(posted.status_code, 200, posted.data)
        return response.data["id"]

    def test_one_receipt_allocates_multiple_invoices_and_advance_can_be_allocated_later(self):
        first_invoice = self._create_posted_invoice("BATCH-INV-001")
        second_invoice = self._create_posted_invoice("BATCH-INV-002")

        created = self.client.post("/api/sales-documents/receipt-batches/", {
            "company": self.company.pk,
            "customer": self.customer.pk,
            "branch": self.branch.pk,
            "receipt_number": "BATCH-RC-001",
            "receipt_date": "2026-03-11",
            "method": "cash",
            "amount": "150",
            "allocations": [
                {"invoice": first_invoice, "amount": "100"},
                {"invoice": second_invoice, "amount": "50"},
            ],
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        self.assertEqual(created.data["allocated_amount"], "150.0000")
        self.assertEqual(created.data["unallocated_amount"], "0.0000")

        posted = self.client.post(
            f"/api/sales-documents/receipt-batches/{created.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(posted.status_code, 200, posted.data)
        statement = self.client.get(
            f"/api/sales-documents/invoices/customer-statement/?company={self.company.pk}"
            f"&customer={self.customer.pk}&date_from=2026-03-10&date_to=2026-03-11"
        )
        self.assertEqual(statement.status_code, 200, statement.data)
        batch_events = [row for row in statement.data["rows"] if row["document_type"] == "receipt_batch"]
        self.assertEqual(len(batch_events), 1)
        self.assertEqual(batch_events[0]["credit"], "150.0000")
        open_ended_statement = self.client.get(
            f"/api/sales-documents/invoices/customer-statement/?company={self.company.pk}"
            f"&customer={self.customer.pk}&date_from=2026-03-10"
        )
        self.assertEqual(open_ended_statement.status_code, 200, open_ended_statement.data)
        open_ended_batch = next(
            row for row in open_ended_statement.data["rows"]
            if row["document_type"] == "receipt_batch"
        )
        self.assertIn("BATCH-INV-001", open_ended_batch["description"])
        aging_after_batch = self.client.get(
            f"/api/sales-documents/invoices/customer-aging/?company={self.company.pk}&as_of=2026-03-11"
        )
        self.assertEqual(aging_after_batch.status_code, 200, aging_after_batch.data)
        self.assertEqual(aging_after_batch.data["totals"]["total_due"], "250.0000")
        self.assertEqual(Decimal(aging_after_batch.data["totals"]["unallocated_advances"]), Decimal("0"))
        self.assertEqual(
            self.client.get(f"/api/sales-documents/invoices/{first_invoice}/").data["balance_due"],
            "100.0000",
        )
        self.assertEqual(
            self.client.get(f"/api/sales-documents/invoices/{second_invoice}/").data["balance_due"],
            "150.0000",
        )

        advance = self.client.post("/api/sales-documents/receipt-batches/", {
            "company": self.company.pk,
            "customer": self.customer.pk,
            "branch": self.branch.pk,
            "receipt_number": "BATCH-ADV-001",
            "receipt_date": "2026-03-12",
            "method": "cash",
            "amount": "30",
            "allocations": [],
        }, format="json")
        self.assertEqual(advance.status_code, 201, advance.data)
        self.assertEqual(advance.data["unallocated_amount"], "30.0000")
        posted_advance = self.client.post(
            f"/api/sales-documents/receipt-batches/{advance.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(posted_advance.status_code, 200, posted_advance.data)
        aging_with_advance = self.client.get(
            f"/api/sales-documents/invoices/customer-aging/?company={self.company.pk}&as_of=2026-03-12"
        )
        self.assertEqual(aging_with_advance.status_code, 200, aging_with_advance.data)
        self.assertEqual(aging_with_advance.data["totals"]["unallocated_advances"], "30.0000")

        allocated = self.client.post(
            f"/api/sales-documents/receipt-batches/{advance.data['id']}/allocate/",
            {"invoice": second_invoice, "amount": "30", "allocation_date": "2026-03-13"},
            format="json",
        )
        self.assertEqual(allocated.status_code, 200, allocated.data)
        self.assertEqual(allocated.data["unallocated_amount"], "0.0000")
        historical_aging = self.client.get(
            f"/api/sales-documents/invoices/customer-aging/?company={self.company.pk}&as_of=2026-03-12"
        )
        self.assertEqual(historical_aging.status_code, 200, historical_aging.data)
        self.assertEqual(historical_aging.data["totals"]["unallocated_advances"], "30.0000")
        aging_after_allocation = self.client.get(
            f"/api/sales-documents/invoices/customer-aging/?company={self.company.pk}&as_of=2026-03-13"
        )
        self.assertEqual(aging_after_allocation.status_code, 200, aging_after_allocation.data)
        self.assertEqual(Decimal(aging_after_allocation.data["totals"]["unallocated_advances"]), Decimal("0"))
        self.assertEqual(aging_after_allocation.data["totals"]["total_due"], "220.0000")
        self.assertEqual(
            self.client.get(f"/api/sales-documents/invoices/{second_invoice}/").data["balance_due"],
            "120.0000",
        )

        over_allocation = self.client.post(
            f"/api/sales-documents/receipt-batches/{advance.data['id']}/allocate/",
            {"invoice": second_invoice, "amount": "1"},
            format="json",
        )
        self.assertEqual(over_allocation.status_code, 400, over_allocation.data)

        reversed_advance = self.client.post(
            f"/api/sales-documents/receipt-batches/{advance.data['id']}/reverse/",
            {"entry_date": "2026-03-13"},
            format="json",
        )
        self.assertEqual(reversed_advance.status_code, 200, reversed_advance.data)
        self.assertEqual(reversed_advance.data["status"], "reversed")
        self.assertEqual(
            self.client.get(f"/api/sales-documents/invoices/{second_invoice}/").data["balance_due"],
            "150.0000",
        )

        reversed_batch = self.client.post(
            f"/api/sales-documents/receipt-batches/{created.data['id']}/reverse/",
            {"entry_date": "2026-03-14"},
            format="json",
        )
        self.assertEqual(reversed_batch.status_code, 200, reversed_batch.data)
        self.assertEqual(reversed_batch.data["status"], "reversed")
        for invoice_id in (first_invoice, second_invoice):
            self.assertEqual(
                self.client.get(f"/api/sales-documents/invoices/{invoice_id}/").data["balance_due"],
                "200.0000",
            )

        journal_ids = [
            posted.data["journal_entry"],
            posted_advance.data["journal_entry"],
            reversed_advance.data["reversal_journal_entry"],
            reversed_batch.data["reversal_journal_entry"],
        ]
        entries = JournalEntry.objects.filter(
            company=self.company, source_type=JournalEntry.Source.SALES, pk__in=journal_ids,
        )
        self.assertEqual(entries.count(), 4)
        for entry in entries:
            lines = list(JournalLine.objects.filter(entry=entry))
            debit = sum((line.debit for line in lines), Decimal("0"))
            credit = sum((line.credit for line in lines), Decimal("0"))
            self.assertEqual(debit, credit, entry.entry_number)

    def test_reversing_bank_receipt_batch_also_reverses_its_treasury_mirror(self):
        from accounting.models import ChartOfAccount
        from treasury.models import TreasuryAccount, TreasuryTransaction

        bank_gl = ChartOfAccount.objects.get(company=self.company, code="1100")
        TreasuryAccount.objects.create(
            company=self.company, branch=self.branch, account=bank_gl,
            kind=TreasuryAccount.Kind.BANK, code="BANK-BATCH-REV",
            name_ar="بنك سندات القبض المجمعة", currency_code="EGP",
        )
        created = self.client.post("/api/sales-documents/receipt-batches/", {
            "company": self.company.pk,
            "customer": self.customer.pk,
            "branch": self.branch.pk,
            "receipt_number": "BATCH-BANK-REV-001",
            "receipt_date": "2026-03-11",
            "method": "bank",
            "payment_account": bank_gl.pk,
            "amount": "75",
            "allocations": [],
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        posted = self.client.post(
            f"/api/sales-documents/receipt-batches/{created.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(posted.status_code, 200, posted.data)
        mirror = TreasuryTransaction.objects.get(journal_entry_id=posted.data["journal_entry"])
        self.assertEqual(mirror.status, TreasuryTransaction.Status.POSTED)

        reversed_batch = self.client.post(
            f"/api/sales-documents/receipt-batches/{created.data['id']}/reverse/",
            {"entry_date": "2026-03-12"}, format="json",
        )
        self.assertEqual(reversed_batch.status_code, 200, reversed_batch.data)
        mirror.refresh_from_db()
        self.assertEqual(mirror.status, TreasuryTransaction.Status.REVERSED)
        self.assertEqual(mirror.reversal_journal_entry_id, reversed_batch.data["reversal_journal_entry"])

    def test_customer_refund_reconciles_statement_and_advance_and_blocks_batch_reversal(self):
        from accounting.models import ChartOfAccount
        from sales.models import CustomerRefund
        from treasury.models import TreasuryAccount, TreasuryTransaction

        cash = ChartOfAccount.objects.get(company=self.company, code="1005")
        receivables = ChartOfAccount.objects.get(company=self.company, code="1200")
        from companies.models import FiscalYear
        year = FiscalYear.objects.get(company=self.company, name="2026")
        treasury_account = TreasuryAccount.objects.create(
            company=self.company, branch=self.branch, account=cash,
            kind=TreasuryAccount.Kind.CASH, code="CASH-ADV-REFUND",
            name_ar="خزينة رد الدفعات المقدمة", currency_code="EGP",
        )
        batch_response = self.client.post("/api/sales-documents/receipt-batches/", {
            "company": self.company.pk, "customer": self.customer.pk, "branch": self.branch.pk,
            "receipt_number": "BATCH-REFUND-001", "receipt_date": "2026-03-11",
            "method": "cash", "amount": "100", "allocations": [],
        }, format="json")
        self.assertEqual(batch_response.status_code, 201, batch_response.data)
        batch_posted = self.client.post(
            f"/api/sales-documents/receipt-batches/{batch_response.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(batch_posted.status_code, 200, batch_posted.data)

        refund_entry = JournalEntry.objects.create(
            company=self.company, branch=self.branch, fiscal_year=year,
            entry_number="CUSTOMER-REFUND-001", entry_date=date(2026, 3, 12),
            description="رد جزء من دفعة مقدمة للعميل", source_type=JournalEntry.Source.SALES,
            created_by=self.user,
        )
        JournalLine.objects.create(
            entry=refund_entry, account=receivables, debit=Decimal("25"), credit=0,
        )
        JournalLine.objects.create(
            entry=refund_entry, account=cash, debit=0, credit=Decimal("25"),
        )
        refund_entry = post_journal(refund_entry.pk, actor=self.user)
        refund_movement = TreasuryTransaction.objects.create(
            company=self.company, branch=self.branch, fiscal_year=year,
            source_account=treasury_account, counter_account=receivables,
            kind=TreasuryTransaction.Kind.PAYMENT, status=TreasuryTransaction.Status.POSTED,
            document_number="AUTO-CUSTOMER-REFUND-001", transaction_date=date(2026, 3, 12),
            amount=Decimal("25"), description="رد مبلغ للعميل",
            journal_entry=refund_entry, created_by=self.user,
        )
        refund = CustomerRefund.objects.create(
            company=self.company, customer=self.customer, branch=self.branch,
            source_receipt_batch_id=batch_response.data["id"],
            treasury_account=treasury_account, treasury_transaction=refund_movement,
            refund_number="CUSTOMER-REFUND-001", refund_date=date(2026, 3, 12),
            amount=Decimal("25"), status=CustomerRefund.Status.POSTED, created_by=self.user,
        )

        statement_before_reversal = self.client.get(
            f"/api/sales-documents/invoices/customer-statement/?company={self.company.pk}"
            f"&customer={self.customer.pk}&date_from=2026-03-11&date_to=2026-03-13"
        )
        self.assertEqual(statement_before_reversal.status_code, 200, statement_before_reversal.data)
        self.assertEqual(statement_before_reversal.data["closing_balance"], "-75.0000")
        self.assertIn("customer_refund", [
            row["document_type"] for row in statement_before_reversal.data["rows"]
        ])
        aging_before_reversal = self.client.get(
            f"/api/sales-documents/invoices/customer-aging/?company={self.company.pk}&as_of=2026-03-13"
        )
        self.assertEqual(aging_before_reversal.status_code, 200, aging_before_reversal.data)
        self.assertEqual(aging_before_reversal.data["totals"]["unallocated_advances"], "75.0000")

        blocked = self.client.post(
            f"/api/sales-documents/receipt-batches/{batch_response.data['id']}/reverse/",
            {"entry_date": "2026-03-13"}, format="json",
        )
        self.assertEqual(blocked.status_code, 400, blocked.data)
        self.assertIn("مردودات العميل", str(blocked.data))

        refund_reversal = reverse_operational_journal(
            refund_entry.pk, self.user, date(2026, 3, 14), JournalEntry.Source.SALES,
        )
        TreasuryTransaction.objects.filter(pk=refund_movement.pk).update(
            status=TreasuryTransaction.Status.REVERSED,
            reversal_journal_entry=refund_reversal,
        )
        refund.status = CustomerRefund.Status.REVERSED
        refund.reversal_date = date(2026, 3, 14)
        refund.reversal_journal_entry = refund_reversal
        refund.save(update_fields=("status", "reversal_date", "reversal_journal_entry"))

        statement_after_reversal = self.client.get(
            f"/api/sales-documents/invoices/customer-statement/?company={self.company.pk}"
            f"&customer={self.customer.pk}&date_from=2026-03-11&date_to=2026-03-14"
        )
        self.assertEqual(statement_after_reversal.status_code, 200, statement_after_reversal.data)
        self.assertEqual(statement_after_reversal.data["closing_balance"], "-100.0000")
        self.assertIn("customer_refund_reversal", [
            row["document_type"] for row in statement_after_reversal.data["rows"]
        ])
        aging_after_reversal = self.client.get(
            f"/api/sales-documents/invoices/customer-aging/?company={self.company.pk}&as_of=2026-03-14"
        )
        self.assertEqual(aging_after_reversal.status_code, 200, aging_after_reversal.data)
        self.assertEqual(aging_after_reversal.data["totals"]["unallocated_advances"], "100.0000")

        reversed_batch = self.client.post(
            f"/api/sales-documents/receipt-batches/{batch_response.data['id']}/reverse/",
            {"entry_date": "2026-03-15"}, format="json",
        )
        self.assertEqual(reversed_batch.status_code, 200, reversed_batch.data)
        final_statement = self.client.get(
            f"/api/sales-documents/invoices/customer-statement/?company={self.company.pk}"
            f"&customer={self.customer.pk}&date_from=2026-03-11&date_to=2026-03-15"
        )
        self.assertEqual(final_statement.status_code, 200, final_statement.data)
        self.assertEqual(final_statement.data["closing_balance"], "0.0000")

    def test_direct_receipt_cannot_overcollect_after_batch_allocation(self):
        invoice_id = self._create_posted_invoice("BATCH-INV-DIRECT-OVER")
        batch = self.client.post("/api/sales-documents/receipt-batches/", {
            "company": self.company.pk,
            "customer": self.customer.pk,
            "branch": self.branch.pk,
            "receipt_number": "BATCH-RC-DIRECT-OVER",
            "receipt_date": "2026-03-11",
            "method": "cash",
            "amount": "100",
            "allocations": [{"invoice": invoice_id, "amount": "100"}],
        }, format="json")
        self.assertEqual(batch.status_code, 201, batch.data)
        posted_batch = self.client.post(
            f"/api/sales-documents/receipt-batches/{batch.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(posted_batch.status_code, 200, posted_batch.data)

        direct = self.client.post("/api/sales-documents/receipts/", {
            "company": self.company.pk,
            "customer": self.customer.pk,
            "invoice": invoice_id,
            "receipt_number": "RC-DIRECT-OVER-BATCH",
            "receipt_date": "2026-03-12",
            "method": "cash",
            "amount": "101",
        }, format="json")
        self.assertEqual(direct.status_code, 201, direct.data)
        rejected = self.client.post(
            f"/api/sales-documents/receipts/{direct.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(rejected.status_code, 400, rejected.data)
        self.assertIn("100.0000", str(rejected.data))
        invoice = self.client.get(f"/api/sales-documents/invoices/{invoice_id}/")
        self.assertEqual(invoice.status_code, 200, invoice.data)
        self.assertEqual(invoice.data["balance_due"], "100.0000")


    def test_receipt_batch_rejects_allocations_above_invoice_balance(self):
        invoice_id = self._create_posted_invoice("BATCH-INV-OVER")
        created = self.client.post("/api/sales-documents/receipt-batches/", {
            "company": self.company.pk,
            "customer": self.customer.pk,
            "branch": self.branch.pk,
            "receipt_number": "BATCH-RC-OVER",
            "receipt_date": "2026-03-11",
            "method": "cash",
            "amount": "250",
            "allocations": [{"invoice": invoice_id, "amount": "250"}],
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        posted = self.client.post(
            f"/api/sales-documents/receipt-batches/{created.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(posted.status_code, 400, posted.data)
        self.assertEqual(
            self.client.get(f"/api/sales-documents/invoices/{invoice_id}/").data["balance_due"],
            "200.0000",
        )
    def test_advance_allocation_rejects_non_finite_and_excess_precision(self):
        invoice_id = self._create_posted_invoice("BATCH-INV-PRECISION")
        advance = self.client.post("/api/sales-documents/receipt-batches/", {
            "company": self.company.pk,
            "customer": self.customer.pk,
            "branch": self.branch.pk,
            "receipt_number": "BATCH-ADV-PRECISION",
            "receipt_date": "2026-03-12",
            "method": "cash",
            "amount": "10",
            "allocations": [],
        }, format="json")
        self.assertEqual(advance.status_code, 201, advance.data)
        posted = self.client.post(
            f"/api/sales-documents/receipt-batches/{advance.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(posted.status_code, 200, posted.data)
        for amount in ("NaN", "Infinity", "0.00001"):
            response = self.client.post(
                f"/api/sales-documents/receipt-batches/{advance.data['id']}/allocate/",
                {"invoice": invoice_id, "amount": amount, "allocation_date": "2026-03-13"},
                format="json",
            )
            self.assertEqual(response.status_code, 400, response.data)
