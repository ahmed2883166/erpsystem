from decimal import Decimal

from rest_framework.test import APITestCase

from accounting.models import AccountRole, ChartOfAccount, JournalLine
from companies.models import Currency, ExchangeRate
from inventory.models import Product
from treasury.models import TreasuryAccount, TreasuryFxPosition, TreasuryTransaction


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

    def _post_invoice_receipt(self, invoice, number, amount=None):
        created = self.client.post("/api/sales-documents/receipts/", {
            "company": self.company.pk,
            "customer": self.customer.pk,
            "invoice": invoice["id"],
            "receipt_number": number,
            "receipt_date": "2026-03-11",
            "method": "cash",
            "payment_account": self.cash_account.pk,
            "amount": amount or invoice["grand_total"],
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        posted = self.client.post(
            f"/api/sales-documents/receipts/{created.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(posted.status_code, 200, posted.data)

    def _create_posted_return(self, invoice, number, receipt_amount=None):
        self._post_invoice_receipt(invoice, f"{number}-RECEIPT", receipt_amount)
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
        invoice = self._create_posted_invoice("REFUND-ADV-INV")
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
            {"invoice": invoice["id"], "amount": "31",
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

    def test_return_refund_requires_net_customer_credit_not_just_cash_collected(self):
        invoice = self._create_posted_invoice("REFUND-INV-PARTIAL-COLLECTION")
        receipt = self.client.post("/api/sales-documents/receipts/", {
            "company": self.company.pk,
            "customer": self.customer.pk,
            "invoice": invoice["id"],
            "receipt_number": "REFUND-PARTIAL-COLLECTION-REC",
            "receipt_date": "2026-03-11",
            "method": "cash",
            "payment_account": self.cash_account.pk,
            "amount": "25",
        }, format="json")
        self.assertEqual(receipt.status_code, 201, receipt.data)
        posted_receipt = self.client.post(
            f"/api/sales-documents/receipts/{receipt.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(posted_receipt.status_code, 200, posted_receipt.data)

        created_return = self.client.post("/api/sales-documents/returns/", {
            "company": self.company.pk,
            "invoice": invoice["id"],
            "return_number": "REFUND-RET-PARTIAL-COLLECTION",
            "return_date": "2026-03-12",
            "lines": [{
                "invoice_line": invoice["invoice_lines"][0]["id"],
                "quantity": "1",
            }],
        }, format="json")
        self.assertEqual(created_return.status_code, 201, created_return.data)
        posted_return = self.client.post(
            f"/api/sales-documents/returns/{created_return.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(posted_return.status_code, 200, posted_return.data)

        over_collected = self._create_refund(
            "REFUND-RET-NO-CREDIT", amount="25",
            source_return=posted_return.data["id"],
        )
        self.assertEqual(over_collected.status_code, 201, over_collected.data)
        rejected = self.client.post(
            f"/api/sales-documents/refunds/{over_collected.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(rejected.status_code, 400, rejected.data)

    def test_return_refund_is_limited_to_the_customer_net_credit_balance(self):
        invoice = self._create_posted_invoice("REFUND-INV-NET-CREDIT")
        sales_return = self._create_posted_return(
            invoice, "REFUND-RET-NET-CREDIT", receipt_amount="150",
        )
        self.assertEqual(sales_return["grand_total"], "100.0000")

        over_credit = self._create_refund(
            "REFUND-RET-OVER-NET-CREDIT", amount="50.01",
            source_return=sales_return["id"],
        )
        self.assertEqual(over_credit.status_code, 201, over_credit.data)
        rejected = self.client.post(
            f"/api/sales-documents/refunds/{over_credit.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(rejected.status_code, 400, rejected.data)

        exact_credit = self._create_refund(
            "REFUND-RET-EXACT-NET-CREDIT", amount="50",
            source_return=sales_return["id"],
        )
        self.assertEqual(exact_credit.status_code, 201, exact_credit.data)
        posted = self.client.post(
            f"/api/sales-documents/refunds/{exact_credit.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(posted.status_code, 200, posted.data)
        self.assertEqual(posted.data["amount"], "50.0000")

    def test_refund_cannot_exceed_return_credit_or_unallocated_advance(self):
        invoice = self._create_posted_invoice("REFUND-INV-LIMIT")
        sales_return = self._create_posted_return(invoice, "REFUND-RET-LIMIT")
        too_much_return = self._create_refund(
            "REFUND-RET-OVER", amount="100.01", source_return=sales_return["id"],
        )
        self.assertEqual(too_much_return.status_code, 201, too_much_return.data)
        rejected_return = self.client.post(
            f"/api/sales-documents/refunds/{too_much_return.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(rejected_return.status_code, 400, rejected_return.data)

        batch = self._create_posted_advance("REFUND-ADV-LIMIT", "25")
        too_much_advance = self._create_refund(
            "REFUND-ADV-OVER", amount="25.01", source_batch=batch["id"],
        )
        self.assertEqual(too_much_advance.status_code, 201, too_much_advance.data)
        rejected_advance = self.client.post(
            f"/api/sales-documents/refunds/{too_much_advance.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(rejected_advance.status_code, 400, rejected_advance.data)


    def test_foreign_currency_return_refund_posts_and_reverses_exact_treasury_carrying_value(self):
        usd = Currency.objects.create(code="USD", name_ar="دولار أمريكي", decimal_places=2)
        for rate_date, rate in (
            ("2026-03-10", "50"), ("2026-03-11", "52"), ("2026-03-13", "50"),
        ):
            ExchangeRate.objects.create(
                company=self.company, currency=usd, rate_date=rate_date, rate=Decimal(rate),
            )
        service = Product.objects.create(
            company=self.company, unit=self.product.unit, code="REFUND-FX-SERVICE",
            name_ar="خدمة بالدولار", product_type=Product.ProductType.SERVICE,
            stock_class=None, sale_price=Decimal("100"),
        )
        invoice = self.client.post("/api/sales-documents/invoices/", {
            "company": self.company.pk, "customer": self.customer.pk,
            "branch": self.branch.pk, "invoice_number": "REFUND-FX-INV",
            "invoice_date": "2026-03-10", "transaction_currency": usd.pk,
            "exchange_rate": "50", "exchange_rate_date": "2026-03-10",
            "lines": [{
                "product": service.pk, "quantity": "1",
                "transaction_unit_price": "100", "tax_rate": "0",
            }],
        }, format="json")
        self.assertEqual(invoice.status_code, 201, invoice.data)
        posted_invoice = self.client.post(
            f"/api/sales-documents/invoices/{invoice.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(posted_invoice.status_code, 200, posted_invoice.data)

        receipt = self.client.post("/api/sales-documents/receipts/", {
            "company": self.company.pk, "customer": self.customer.pk,
            "invoice": invoice.data["id"], "receipt_number": "REFUND-FX-RECEIPT",
            "receipt_date": "2026-03-11", "method": "cash",
            "payment_account": self.cash_account.pk, "amount": "5200",
            "transaction_currency": usd.pk, "transaction_amount": "100",
            "exchange_rate": "52", "exchange_rate_date": "2026-03-11",
        }, format="json")
        self.assertEqual(receipt.status_code, 201, receipt.data)
        posted_receipt = self.client.post(
            f"/api/sales-documents/receipts/{receipt.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(posted_receipt.status_code, 200, posted_receipt.data)

        sales_return = self.client.post("/api/sales-documents/returns/", {
            "company": self.company.pk, "invoice": invoice.data["id"],
            "branch": self.branch.pk, "return_number": "REFUND-FX-RETURN",
            "return_date": "2026-03-12",
            "lines": [{"invoice_line": invoice.data["invoice_lines"][0]["id"], "quantity": "1"}],
        }, format="json")
        self.assertEqual(sales_return.status_code, 201, sales_return.data)
        posted_return = self.client.post(
            f"/api/sales-documents/returns/{sales_return.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(posted_return.status_code, 200, posted_return.data)
        self.assertEqual(posted_return.data["transaction_grand_total"], "100.0000")

        foreign_gl = ChartOfAccount.objects.create(
            company=self.company, code="USD-REFUND-BANK", name_ar="بنك الدولار للاختبار",
            nature=ChartOfAccount.Nature.ASSET,
        )
        foreign_treasury = TreasuryAccount.objects.create(
            company=self.company, branch=self.branch, account=foreign_gl,
            kind=TreasuryAccount.Kind.BANK, code="USD-REFUND",
            name_ar="بنك الدولار", currency_code="USD",
        )
        position = TreasuryFxPosition.objects.create(
            account=foreign_treasury, foreign_amount=Decimal("100"),
            carrying_base_amount=Decimal("4800"),
        )

        over_refund = self.client.post("/api/sales-documents/refunds/", {
            "company": self.company.pk, "customer": self.customer.pk,
            "refund_number": "REFUND-FX-OVER", "refund_date": "2026-03-13",
            "source_return": posted_return.data["id"],
            "treasury_account": foreign_treasury.pk,
            "transaction_currency": usd.pk, "transaction_amount": "100.01",
            "exchange_rate": "50", "exchange_rate_date": "2026-03-13",
        }, format="json")
        self.assertEqual(over_refund.status_code, 201, over_refund.data)
        rejected = self.client.post(
            f"/api/sales-documents/refunds/{over_refund.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(rejected.status_code, 400, rejected.data)

        created = self.client.post("/api/sales-documents/refunds/", {
            "company": self.company.pk, "customer": self.customer.pk,
            "refund_number": "REFUND-FX-VALID", "refund_date": "2026-03-13",
            "source_return": posted_return.data["id"],
            "treasury_account": foreign_treasury.pk,
            "transaction_currency": usd.pk, "transaction_amount": "100",
            "exchange_rate": "50", "exchange_rate_date": "2026-03-13",
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        self.assertEqual(created.data["amount"], "5000.0000")
        posted = self.client.post(
            f"/api/sales-documents/refunds/{created.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(posted.status_code, 200, posted.data)
        self.assertEqual(posted.data["transaction_currency_code"], "USD")
        self.assertEqual(posted.data["transaction_amount"], "100.0000")
        original_treasury = TreasuryTransaction.objects.get(pk=posted.data["treasury_transaction"])
        self.assertEqual(original_treasury.carrying_amount, Decimal("4800.0000"))
        self._assert_balanced(original_treasury.journal_entry_id)
        position.refresh_from_db()
        self.assertEqual(position.foreign_amount, Decimal("0.0000"))
        self.assertEqual(position.carrying_base_amount, Decimal("0.0000"))

        reversed_refund = self.client.post(
            f"/api/sales-documents/refunds/{created.data['id']}/reverse/",
            {"entry_date": "2026-03-14"}, format="json",
        )
        self.assertEqual(reversed_refund.status_code, 200, reversed_refund.data)
        reversal = TreasuryTransaction.objects.get(pk=reversed_refund.data["reversal_treasury_transaction"])
        self.assertEqual(reversal.reversal_of_id, original_treasury.pk)
        self.assertEqual(reversal.carrying_amount, Decimal("4800.0000"))
        self._assert_balanced(reversal.journal_entry_id)
        position.refresh_from_db()
        self.assertEqual(position.foreign_amount, Decimal("100.0000"))
        self.assertEqual(position.carrying_base_amount, Decimal("4800.0000"))
