from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from accounting.models import AccountRole, ChartOfAccount, JournalEntry
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

    def _create_delivery(self):
        response = self.client.post("/api/sales-documents/deliveries/", {
            "company": self.company.pk, "customer": self.customer.pk, "warehouse": self.warehouse.pk,
            "delivery_number": "DEL-001", "delivery_date": "2026-03-10",
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
