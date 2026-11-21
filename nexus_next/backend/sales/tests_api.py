from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from accounting.models import AccountRole, ChartOfAccount, JournalEntry, TaxCode
from companies.models import Branch, Company, CompanyMembership, Currency, FiscalYear
from customers.models import Customer
from inventory.models import Product, StockBalance, StockMovement, StockMovementLine, UnitOfMeasure, Warehouse
from inventory.services import post_stock_movement


class SalesWorkflowTests(APITestCase):
    def setUp(self):
        currency = Currency.objects.create(code="EGP", name_ar="جنيه مصري")
        self.company = Company.objects.create(code="SELL", name_ar="شركة مبيعات", country="EG", activity="commercial", base_currency=currency)
        self.branch = Branch.objects.create(company=self.company, code="MAIN", name_ar="الرئيسي", is_head_office=True)
        FiscalYear.objects.create(company=self.company, name="2026", start_date=date(2026, 1, 1), end_date=date(2026, 12, 31))
        self.user = get_user_model().objects.create_user(username="seller", password="test-password")
        CompanyMembership.objects.create(user=self.user, company=self.company, role="accountant", all_branches=True)
        self.client.force_authenticate(self.user)
        self.customer = Customer.objects.create(company=self.company, code="CUS-1", name_ar="العميل الأول")
        unit = UnitOfMeasure.objects.create(company=self.company, code="PCS", name_ar="قطعة")
        self.product = Product.objects.create(company=self.company, unit=unit, code="P-1", name_ar="صنف 1")
        self.warehouse = Warehouse.objects.create(company=self.company, branch=self.branch, code="WH1", name_ar="المخزن الرئيسي")
        roles = {
            AccountRole.Role.INVENTORY: ("1000", "المخزون", ChartOfAccount.Nature.ASSET),
            AccountRole.Role.COGS: ("5000", "تكلفة المبيعات", ChartOfAccount.Nature.EXPENSE),
            AccountRole.Role.GOODS_RECEIVED_NOT_INVOICED: ("2100", "بضاعة مستلمة غير مفوترة", ChartOfAccount.Nature.LIABILITY),
            AccountRole.Role.ACCOUNTS_RECEIVABLE: ("1200", "العملاء", ChartOfAccount.Nature.ASSET),
            AccountRole.Role.SALES: ("4000", "المبيعات", ChartOfAccount.Nature.REVENUE),
            AccountRole.Role.VAT_OUTPUT: ("2200", "ضريبة مخرجات", ChartOfAccount.Nature.LIABILITY),
            AccountRole.Role.BANK: ("1100", "البنك", ChartOfAccount.Nature.ASSET),
        }
        for role, (code, name, nature) in roles.items():
            account = ChartOfAccount.objects.create(company=self.company, code=code, name_ar=name, nature=nature)
            AccountRole.objects.create(company=self.company, role=role, account=account)
        receipt = StockMovement.objects.create(
            company=self.company, movement_number="OPEN-STOCK-1",
            movement_type=StockMovement.MovementType.RECEIPT, movement_date=date(2026, 3, 1),
            destination_warehouse=self.warehouse, created_by=self.user,
        )
        StockMovementLine.objects.create(
            movement=receipt, product=self.product, quantity=5, unit_cost=40,
        )
        post_stock_movement(receipt.pk, self.user)

    def _create_delivery(self, number="DEL-001"):
        response = self.client.post("/api/sales-documents/deliveries/", {
            "company": self.company.pk, "customer": self.customer.pk, "warehouse": self.warehouse.pk,
            "delivery_number": number, "delivery_date": "2026-03-10",
            "lines": [{"product": self.product.pk, "quantity": "2"}],
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        delivery_id = response.data["id"]
        line_id = response.data["line_items"][0]["id"]
        posted = self.client.post(f"/api/sales-documents/deliveries/{delivery_id}/post/", {}, format="json")
        self.assertEqual(posted.status_code, 200, posted.data)
        return delivery_id, line_id

    def test_delivery_invoice_and_customer_receipt_post_once(self):
        delivery_id, line_id = self._create_delivery()
        balance = StockBalance.objects.get(product=self.product, warehouse=self.warehouse)
        self.assertEqual(balance.quantity, Decimal("3"))
        self.assertEqual(balance.inventory_value, Decimal("120.0000"))
        invoice_response = self.client.post("/api/sales-documents/invoices/", {
            "company": self.company.pk, "customer": self.customer.pk, "delivery": delivery_id,
            "invoice_number": "INV-001", "invoice_date": "2026-03-10",
            "lines": [{"delivery_line": line_id, "unit_price": "100", "tax_rate": "14"}],
        }, format="json")
        self.assertEqual(invoice_response.status_code, 201, invoice_response.data)
        invoice_id = invoice_response.data["id"]
        posted = self.client.post(f"/api/sales-documents/invoices/{invoice_id}/post/", {}, format="json")
        self.assertEqual(posted.status_code, 200, posted.data)
        self.assertEqual(posted.data["grand_total"], "228.0000")
        self.assertEqual(posted.data["balance_due"], "228.0000")
        receipt_response = self.client.post("/api/sales-documents/receipts/", {
            "company": self.company.pk, "customer": self.customer.pk, "invoice": invoice_id,
            "receipt_number": "RC-001", "receipt_date": "2026-03-11",
            "method": "bank", "amount": "100",
        }, format="json")
        self.assertEqual(receipt_response.status_code, 201, receipt_response.data)
        receipt_id = receipt_response.data["id"]
        posted_receipt = self.client.post(f"/api/sales-documents/receipts/{receipt_id}/post/", {}, format="json")
        self.assertEqual(posted_receipt.status_code, 200, posted_receipt.data)
        invoice = self.client.get(f"/api/sales-documents/invoices/{invoice_id}/").data
        self.assertEqual(invoice["paid_total"], "100.0000")
        self.assertEqual(invoice["balance_due"], "128.0000")
        self.assertEqual(StockBalance.objects.get(product=self.product, warehouse=self.warehouse).quantity, Decimal("3"))
        self.assertEqual(JournalEntry.objects.filter(source_type=JournalEntry.Source.SALES, status="posted").count(), 2)

    def test_customer_receipt_cannot_exceed_invoice_balance(self):
        delivery_id, line_id = self._create_delivery()
        invoice = self.client.post("/api/sales-documents/invoices/", {
            "company": self.company.pk, "customer": self.customer.pk, "delivery": delivery_id,
            "invoice_number": "INV-002", "invoice_date": "2026-03-10",
            "lines": [{"delivery_line": line_id, "unit_price": "100", "tax_rate": "0"}],
        }, format="json")
        self.assertEqual(invoice.status_code, 201, invoice.data)
        invoice_id = invoice.data["id"]
        self.client.post(f"/api/sales-documents/invoices/{invoice_id}/post/", {}, format="json")
        receipt = self.client.post("/api/sales-documents/receipts/", {
            "company": self.company.pk, "customer": self.customer.pk, "invoice": invoice_id,
            "receipt_number": "RC-OVER", "receipt_date": "2026-03-11",
            "method": "bank", "amount": "201",
        }, format="json")
        self.assertEqual(receipt.status_code, 201, receipt.data)
        rejected = self.client.post(f"/api/sales-documents/receipts/{receipt.data['id']}/post/", {}, format="json")
        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(JournalEntry.objects.filter(source_type=JournalEntry.Source.SALES, status="posted").count(), 1)


    def _create_posted_invoice(self, number, invoice_date):
        delivery_id, line_id = self._create_delivery(f"DEL-{number}")
        response = self.client.post("/api/sales-documents/invoices/", {
            "company": self.company.pk, "customer": self.customer.pk, "delivery": delivery_id,
            "invoice_number": number, "invoice_date": invoice_date,
            "lines": [{"delivery_line": line_id, "unit_price": "100", "tax_rate": "0"}],
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        invoice_id = response.data["id"]
        posted = self.client.post(f"/api/sales-documents/invoices/{invoice_id}/post/", {}, format="json")
        self.assertEqual(posted.status_code, 200, posted.data)
        return invoice_id

    def _post_customer_receipt(self, invoice_id, number, receipt_date, amount):
        invoice = self.client.get(f"/api/sales-documents/invoices/{invoice_id}/").data
        response = self.client.post("/api/sales-documents/receipts/", {
            "company": self.company.pk, "customer": self.customer.pk, "invoice": invoice_id,
            "receipt_number": number, "receipt_date": receipt_date, "method": "bank",
            "amount": str(amount),
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        posted = self.client.post(f"/api/sales-documents/receipts/{response.data['id']}/post/", {}, format="json")
        self.assertEqual(posted.status_code, 200, posted.data)

    def test_customer_aging_buckets_and_as_of_partial_receipts(self):
        # Supply enough stock for the five independent delivery/invoice documents.
        stock = StockMovement.objects.create(
            company=self.company, movement_number="AGING-STOCK",
            movement_type=StockMovement.MovementType.RECEIPT, movement_date=date(2026, 1, 1),
            destination_warehouse=self.warehouse, created_by=self.user,
        )
        StockMovementLine.objects.create(movement=stock, product=self.product, quantity=20, unit_cost=40)
        post_stock_movement(stock.pk, self.user)
        self.customer.payment_terms_days = 10
        self.customer.save(update_fields=("payment_terms_days",))

        not_due = self._create_posted_invoice("AGE-0", "2026-06-25")
        one_to_thirty = self._create_posted_invoice("AGE-1", "2026-06-10")
        thirty_one_to_sixty = self._create_posted_invoice("AGE-2", "2026-05-20")
        sixty_one_to_ninety = self._create_posted_invoice("AGE-3", "2026-04-20")
        over_ninety = self._create_posted_invoice("AGE-4", "2026-03-01")
        self._post_customer_receipt(one_to_thirty, "AGE-R1", "2026-06-20", "50")
        # A receipt after the report date must not reduce the historical balance.
        self._post_customer_receipt(one_to_thirty, "AGE-R2", "2026-07-02", "25")

        response = self.client.get(
            f"/api/sales-documents/invoices/customer-aging/?company={self.company.pk}&as_of=2026-06-30"
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["totals"]["not_due"], "200.0000")
        self.assertEqual(response.data["totals"]["1_30"], "150.0000")
        self.assertEqual(response.data["totals"]["31_60"], "200.0000")
        self.assertEqual(response.data["totals"]["61_90"], "200.0000")
        self.assertEqual(response.data["totals"]["over_90"], "200.0000")
        self.assertEqual(response.data["totals"]["total_due"], "950.0000")
        invoice_rows = {
            row["invoice_number"]: row
            for customer in response.data["customers"]
            for row in customer["invoices"]
        }
        self.assertEqual(invoice_rows["AGE-1"]["balance_due"], "150.0000")
        self.assertEqual(invoice_rows["AGE-1"]["overdue_days"], 10)
        self.assertEqual(invoice_rows["AGE-0"]["bucket"], "not_due")
        self.assertEqual(len(invoice_rows), 5)

    def test_customer_aging_is_company_and_branch_scoped(self):
        response = self.client.get(
            f"/api/sales-documents/invoices/customer-aging/?company={self.company.pk}&branch={self.branch.pk}&as_of=2026-06-30"
        )
        self.assertEqual(response.status_code, 200, response.data)
        other = Company.objects.create(
            code="OTHER-AGE", name_ar="شركة أخرى", country="EG",
            activity="commercial", base_currency=self.company.base_currency,
        )
        denied = self.client.get(
            f"/api/sales-documents/invoices/customer-aging/?company={other.pk}&as_of=2026-06-30"
        )
        self.assertEqual(denied.status_code, 403)


    def test_sales_invoice_uses_configured_tax_code_rate_and_validity(self):
        delivery_id, line_id = self._create_delivery("DEL-TAX-1")
        code = TaxCode.objects.create(
            company=self.company, country="EG", code="VAT-14", name_ar="قيمة مضافة",
            category=TaxCode.Category.STANDARD, rate=Decimal("14"),
            effective_from=date(2026, 1, 1), input_tax_recoverable=True,
        )
        invoice = self.client.post("/api/sales-documents/invoices/", {
            "company": self.company.pk, "customer": self.customer.pk, "delivery": delivery_id,
            "invoice_number": "INV-TAX-1", "invoice_date": "2026-03-10",
            "lines": [{"delivery_line": line_id, "unit_price": "100", "tax_code": code.pk}],
        }, format="json")
        self.assertEqual(invoice.status_code, 201, invoice.data)
        self.assertEqual(invoice.data["invoice_lines"][0]["tax_rate"], "14.0000")
        self.assertEqual(invoice.data["invoice_lines"][0]["tax_code"], code.pk)
        self.assertEqual(invoice.data["tax_total"], "28.0000")

    def test_sales_invoice_rejects_tax_code_outside_effective_period(self):
        delivery_id, line_id = self._create_delivery("DEL-TAX-2")
        code = TaxCode.objects.create(
            company=self.company, country="EG", code="VAT-FUTURE", name_ar="ضريبة مستقبلية",
            category=TaxCode.Category.STANDARD, rate=Decimal("14"),
            effective_from=date(2027, 1, 1),
        )
        response = self.client.post("/api/sales-documents/invoices/", {
            "company": self.company.pk, "customer": self.customer.pk, "delivery": delivery_id,
            "invoice_number": "INV-TAX-2", "invoice_date": "2026-03-10",
            "lines": [{"delivery_line": line_id, "unit_price": "100", "tax_code": code.pk}],
        }, format="json")
        self.assertEqual(response.status_code, 400)
