from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.db import connection, DatabaseError, transaction
from django.db.models.query import QuerySet
from unittest.mock import patch

from rest_framework.test import APITestCase

from accounting.models import AccountRole, ChartOfAccount, JournalEntry, JournalLine, TaxCode
from approvals.models import DiscountApprovalPolicy, SalesInvoiceApproval
from companies.access import resolve_warehouse_branch
from companies.models import Branch, Company, CompanyMembership, Currency, ExchangeRate, FiscalYear
from customers.models import Customer, CustomerDiscountRule
from sales.models import CustomerReceipt, CustomerReceiptBatch, SalesInvoice, SalesInvoiceLine, SalesQuotation, SalesDocumentTemplate
from inventory.models import Product, StockBalance, StockCostConsumption, StockMovement, StockMovementLine, UnitOfMeasure, Warehouse
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
            AccountRole.Role.SALES_RETURNS: ("4090", "مردودات المبيعات", ChartOfAccount.Nature.REVENUE),
            AccountRole.Role.VAT_OUTPUT: ("2200", "ضريبة مخرجات", ChartOfAccount.Nature.LIABILITY),
            AccountRole.Role.BANK: ("1100", "البنك", ChartOfAccount.Nature.ASSET),
            AccountRole.Role.CASH: ("1005", "الصندوق", ChartOfAccount.Nature.ASSET),
            AccountRole.Role.REALIZED_FX_GAIN: ("4900", "أرباح فروق عملة محققة", ChartOfAccount.Nature.REVENUE),
            AccountRole.Role.REALIZED_FX_LOSS: ("6900", "خسائر فروق عملة محققة", ChartOfAccount.Nature.EXPENSE),
            AccountRole.Role.UNREALIZED_FX_GAIN: ("7210", "أرباح فروق عملة غير محققة", ChartOfAccount.Nature.REVENUE),
            AccountRole.Role.UNREALIZED_FX_LOSS: ("6160", "خسائر فروق عملة غير محققة", ChartOfAccount.Nature.EXPENSE),
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

    def _create_delivery(self, number="DEL-001", delivery_date="2026-03-10"):
        response = self.client.post("/api/sales-documents/deliveries/", {
            "company": self.company.pk, "customer": self.customer.pk, "warehouse": self.warehouse.pk,
            "delivery_number": number, "delivery_date": delivery_date,
            "lines": [{"product": self.product.pk, "quantity": "2"}],
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        delivery_id = response.data["id"]
        line_id = response.data["line_items"][0]["id"]
        posted = self.client.post(f"/api/sales-documents/deliveries/{delivery_id}/post/", {}, format="json")
        self.assertEqual(posted.status_code, 200, posted.data)
        return delivery_id, line_id

    def test_unassigned_sales_warehouse_uses_same_branch_for_validation_and_posting(self):
        from sales.services import _branch_and_year

        warehouse = Warehouse.objects.create(
            company=self.company, branch=None, code="WH-NO-BRANCH", name_ar="مخزن بلا فرع",
        )
        resolved = resolve_warehouse_branch(self.company, warehouse)
        posting_branch, _ = _branch_and_year(
            SimpleNamespace(company=self.company, warehouse=warehouse), date(2026, 3, 10),
        )
        self.assertEqual(resolved.pk, self.branch.pk)
        self.assertEqual(posting_branch.pk, resolved.pk)

    def test_branch_limited_user_cannot_use_unassigned_sales_warehouse_outside_default_branch(self):
        restricted_branch = Branch.objects.create(
            company=self.company, code="SEC", name_ar="فرع محدود",
        )
        shared = Warehouse.objects.create(
            company=self.company, branch=None, code="WH-GLOBAL", name_ar="مخزن غير مسند",
        )
        limited = get_user_model().objects.create_user(username="sales-branch-limited", password="test-password")
        membership = CompanyMembership.objects.create(
            user=limited, company=self.company, role="accountant", all_branches=False,
        )
        membership.branches.add(restricted_branch)
        self.client.force_authenticate(limited)
        created = self.client.post("/api/sales-documents/deliveries/", {
            "company": self.company.pk, "customer": self.customer.pk, "warehouse": shared.pk,
            "delivery_number": "DEL-BRANCH-BLOCK", "delivery_date": "2026-03-10",
            "lines": [{"product": self.product.pk, "quantity": "1"}],
        }, format="json")
        self.assertEqual(created.status_code, 403, created.data)

    def test_draft_delivery_can_be_corrected_and_deleted_safely(self):
        created = self.client.post("/api/sales-documents/deliveries/", {
            "company": self.company.pk, "customer": self.customer.pk, "warehouse": self.warehouse.pk,
            "delivery_number": "DEL-DRAFT-EDIT", "delivery_date": "2026-03-10",
            "notes": "قبل التعديل", "lines": [{"product": self.product.pk, "quantity": "1"}],
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        delivery_id = created.data["id"]
        updated = self.client.patch(
            f"/api/sales-documents/deliveries/{delivery_id}/",
            {"notes": "بعد التعديل", "lines": [{"product": self.product.pk, "quantity": "2"}]},
            format="json",
        )
        self.assertEqual(updated.status_code, 200, updated.data)
        self.assertEqual(updated.data["notes"], "بعد التعديل")
        self.assertEqual(updated.data["line_items"][0]["quantity"], "2.0000")
        movement_id = StockMovement.objects.get(sales_delivery__pk=delivery_id).pk
        # حذف المستندات محصور في مدير الشركة وفق صلاحيات دورة المبيعات.
        admin_user = get_user_model().objects.create_user(username="sales-draft-admin", password="test-password")
        CompanyMembership.objects.create(
            user=admin_user, company=self.company, role="admin", all_branches=True,
        )
        self.client.force_authenticate(admin_user)
        deleted = self.client.delete(f"/api/sales-documents/deliveries/{delivery_id}/")
        self.assertEqual(deleted.status_code, 204)
        self.assertFalse(StockMovement.objects.filter(pk=movement_id).exists())

    def test_sales_invoice_date_cannot_precede_delivery_date_at_create_or_post(self):
        delivery_id, line_id = self._create_delivery("DEL-INVOICE-CHRONOLOGY")

        rejected = self.client.post("/api/sales-documents/invoices/", {
            "company": self.company.pk, "customer": self.customer.pk, "delivery": delivery_id,
            "invoice_number": "INV-BEFORE-DELIVERY", "invoice_date": "2026-03-09",
            "lines": [{"delivery_line": line_id, "unit_price": "100", "tax_rate": "0"}],
        }, format="json")
        self.assertEqual(rejected.status_code, 400, rejected.data)
        self.assertIn("invoice_date", rejected.data)

        created = self.client.post("/api/sales-documents/invoices/", {
            "company": self.company.pk, "customer": self.customer.pk, "delivery": delivery_id,
            "invoice_number": "INV-POST-CHRONOLOGY", "invoice_date": "2026-03-10",
            "lines": [{"delivery_line": line_id, "unit_price": "100", "tax_rate": "0"}],
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)

        # Defend the posting service too if an invalid draft reaches it outside serializer validation.
        SalesInvoice.objects.filter(pk=created.data["id"]).update(invoice_date=date(2026, 3, 9))
        posted = self.client.post(
            f"/api/sales-documents/invoices/{created.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(posted.status_code, 400, posted.data)
        self.assertFalse(JournalEntry.objects.filter(
            description__contains="INV-POST-CHRONOLOGY"
        ).exists())

    def test_draft_sales_invoice_can_be_deleted_only_without_financial_dependencies(self):
        delivery_id, line_id = self._create_delivery("DEL-INVOICE-DELETE")
        payload = {
            "company": self.company.pk, "customer": self.customer.pk, "delivery": delivery_id,
            "invoice_number": "INV-DRAFT-DELETE", "invoice_date": "2026-03-10",
            "lines": [{"delivery_line": line_id, "unit_price": "100", "tax_rate": "14"}],
        }
        created = self.client.post("/api/sales-documents/invoices/", payload, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        draft_id = created.data["id"]

        admin = get_user_model().objects.create_user(username="seller-admin-delete", password="test-password")
        CompanyMembership.objects.create(
            user=admin, company=self.company, role="admin", all_branches=True,
        )
        self.client.force_authenticate(admin)
        deleted = self.client.delete(f"/api/sales-documents/invoices/{draft_id}/")
        self.assertEqual(deleted.status_code, 204, getattr(deleted, "data", None))
        self.assertEqual(self.client.get(f"/api/sales-documents/invoices/{draft_id}/").status_code, 404)
        self.assertEqual(self.client.get(f"/api/sales-documents/deliveries/{delivery_id}/").status_code, 200)

        payload["invoice_number"] = "INV-POSTED-KEEP"
        posted_draft = self.client.post("/api/sales-documents/invoices/", payload, format="json")
        self.assertEqual(posted_draft.status_code, 201, posted_draft.data)
        posted_id = posted_draft.data["id"]
        posted = self.client.post(
            f"/api/sales-documents/invoices/{posted_id}/post/", {}, format="json"
        )
        self.assertEqual(posted.status_code, 200, posted.data)
        rejected = self.client.delete(f"/api/sales-documents/invoices/{posted_id}/")
        self.assertEqual(rejected.status_code, 400, rejected.data)
        self.assertEqual(self.client.get(f"/api/sales-documents/invoices/{posted_id}/").status_code, 200)

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
        alternate_bank = ChartOfAccount.objects.create(
            company=self.company, code="1110", name_ar="البنك الفرعي", nature=ChartOfAccount.Nature.ASSET
        )
        from treasury.models import TreasuryAccount
        TreasuryAccount.objects.create(company=self.company, branch=self.branch, account=alternate_bank, kind=TreasuryAccount.Kind.BANK,
            code="BANK-2", name_ar="البنك الفرعي", currency_code="EGP")
        receipt_response = self.client.post("/api/sales-documents/receipts/", {
            "company": self.company.pk, "customer": self.customer.pk, "invoice": invoice_id,
            "receipt_number": "RC-001", "receipt_date": "2026-03-11",
            "method": "bank", "payment_account": alternate_bank.pk, "amount": "100",
        }, format="json")
        self.assertEqual(receipt_response.status_code, 201, receipt_response.data)
        receipt_id = receipt_response.data["id"]
        posted_receipt = self.client.post(f"/api/sales-documents/receipts/{receipt_id}/post/", {}, format="json")
        self.assertEqual(posted_receipt.status_code, 200, posted_receipt.data)
        self.assertEqual(posted_receipt.data["payment_account"], alternate_bank.pk)
        from accounting.models import JournalLine
        self.assertTrue(JournalLine.objects.filter(
            entry_id=posted_receipt.data["journal_entry"], account=alternate_bank,
            debit=Decimal("100.0000"),
        ).exists())
        invoice = self.client.get(f"/api/sales-documents/invoices/{invoice_id}/").data
        self.assertEqual(invoice["paid_total"], "100.0000")
        self.assertEqual(invoice["balance_due"], "128.0000")
        self.assertEqual(StockBalance.objects.get(product=self.product, warehouse=self.warehouse).quantity, Decimal("3"))
        self.assertEqual(JournalEntry.objects.filter(source_type=JournalEntry.Source.SALES, status="posted").count(), 2)

        # A treasury account assigned to another branch must be rejected cleanly.
        other_branch = Branch.objects.create(company=self.company, code="OTHER", name_ar="فرع آخر")
        other_bank = ChartOfAccount.objects.create(
            company=self.company, code="1120", name_ar="بنك الفرع الآخر", nature=ChartOfAccount.Nature.ASSET
        )
        TreasuryAccount.objects.create(
            company=self.company, branch=other_branch, account=other_bank, kind=TreasuryAccount.Kind.BANK,
            code="BANK-OTHER", name_ar="بنك الفرع الآخر", currency_code="EGP",
        )
        wrong_branch_receipt = self.client.post("/api/sales-documents/receipts/", {
            "company": self.company.pk, "customer": self.customer.pk, "invoice": invoice_id,
            "receipt_number": "RC-OTHER", "receipt_date": "2026-03-12",
            "method": "bank", "payment_account": other_bank.pk, "amount": "10",
        }, format="json")
        self.assertEqual(wrong_branch_receipt.status_code, 400, wrong_branch_receipt.data)
        self.assertIn("payment_account", wrong_branch_receipt.data)

        # Delivery owns the inventory/COGS posting; invoice owns revenue/VAT/receivable.
        inventory_account = AccountRole.objects.get(company=self.company, role=AccountRole.Role.INVENTORY).account
        cogs_account = AccountRole.objects.get(company=self.company, role=AccountRole.Role.COGS).account
        sales_account = AccountRole.objects.get(company=self.company, role=AccountRole.Role.SALES).account
        from accounting.models import JournalLine
        self.assertEqual(
            JournalEntry.objects.filter(source_type=JournalEntry.Source.INVENTORY, status="posted").count(),
            2,  # opening receipt + delivery issue
        )
        self.assertTrue(JournalLine.objects.filter(
            entry__source_type=JournalEntry.Source.INVENTORY,
            account=cogs_account, debit=Decimal("80.0000"),
        ).exists())
        self.assertTrue(JournalLine.objects.filter(
            entry__source_type=JournalEntry.Source.INVENTORY,
            account=inventory_account, credit=Decimal("80.0000"),
        ).exists())
        self.assertTrue(JournalLine.objects.filter(
            entry_id=posted.data["journal_entry"], account=sales_account,
            credit=Decimal("200.0000"),
        ).exists())

        # Retrying a posted delivery must not issue stock or create another journal.
        repeated_delivery = self.client.post(
            f"/api/sales-documents/deliveries/{delivery_id}/post/", {}, format="json"
        )
        self.assertEqual(repeated_delivery.status_code, 400)
        self.assertEqual(StockBalance.objects.get(product=self.product, warehouse=self.warehouse).quantity, Decimal("3"))
        self.assertEqual(
            JournalEntry.objects.filter(source_type=JournalEntry.Source.INVENTORY, status="posted").count(),
            2,
        )

    def test_delivery_quantity_can_be_invoiced_in_multiple_partial_invoices(self):
        delivery_id, line_id = self._create_delivery("DEL-PARTIAL-BILLING")

        first = self.client.post("/api/sales-documents/invoices/", {
            "company": self.company.pk, "customer": self.customer.pk, "delivery": delivery_id,
            "invoice_number": "INV-PARTIAL-1", "invoice_date": "2026-03-10",
            "lines": [{"delivery_line": line_id, "quantity": "0.7500", "unit_price": "100", "tax_rate": "0"}],
        }, format="json")
        self.assertEqual(first.status_code, 201, first.data)
        self.assertEqual(first.data["invoice_lines"][0]["quantity"], "0.7500")
        first_posted = self.client.post(
            f"/api/sales-documents/invoices/{first.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(first_posted.status_code, 200, first_posted.data)

        second = self.client.post("/api/sales-documents/invoices/", {
            "company": self.company.pk, "customer": self.customer.pk, "delivery": delivery_id,
            "invoice_number": "INV-PARTIAL-2", "invoice_date": "2026-03-10",
            "lines": [{"delivery_line": line_id, "quantity": "1.2500", "unit_price": "100", "tax_rate": "0"}],
        }, format="json")
        self.assertEqual(second.status_code, 201, second.data)
        second_posted = self.client.post(
            f"/api/sales-documents/invoices/{second.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(second_posted.status_code, 200, second_posted.data)

        overbilled = self.client.post("/api/sales-documents/invoices/", {
            "company": self.company.pk, "customer": self.customer.pk, "delivery": delivery_id,
            "invoice_number": "INV-PARTIAL-OVER", "invoice_date": "2026-03-10",
            "lines": [{"delivery_line": line_id, "quantity": "0.0001", "unit_price": "100", "tax_rate": "0"}],
        }, format="json")
        self.assertEqual(overbilled.status_code, 400, overbilled.data)
        self.assertEqual(
            SalesInvoice.objects.filter(delivery_id=delivery_id, status=SalesInvoice.Status.POSTED).count(),
            2,
        )
        self.assertEqual(
            sum(SalesInvoice.objects.get(pk=invoice_id).lines.get().quantity for invoice_id in (first.data["id"], second.data["id"])),
            Decimal("2.0000"),
        )

    def test_invoice_posting_rechecks_delivery_quantity_reservations(self):
        delivery_id, line_id = self._create_delivery("DEL-POST-RESERVATION-CHECK")
        first = self.client.post("/api/sales-documents/invoices/", {
            "company": self.company.pk, "customer": self.customer.pk, "delivery": delivery_id,
            "invoice_number": "INV-POST-RESERVE-1", "invoice_date": "2026-03-10",
            "lines": [{"delivery_line": line_id, "quantity": "1.5000", "unit_price": "100", "tax_rate": "0"}],
        }, format="json")
        self.assertEqual(first.status_code, 201, first.data)
        second = self.client.post("/api/sales-documents/invoices/", {
            "company": self.company.pk, "customer": self.customer.pk, "delivery": delivery_id,
            "invoice_number": "INV-POST-RESERVE-2", "invoice_date": "2026-03-10",
            "lines": [{"delivery_line": line_id, "quantity": "0.5000", "unit_price": "100", "tax_rate": "0"}],
        }, format="json")
        self.assertEqual(second.status_code, 201, second.data)

        # Simulate an out-of-band write so posting must independently enforce the total.
        SalesInvoiceLine.objects.filter(invoice_id=first.data["id"]).update(quantity=Decimal("1.5001"))
        rejected = self.client.post(
            f"/api/sales-documents/invoices/{first.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(rejected.status_code, 400, rejected.data)
        self.assertFalse(JournalEntry.objects.filter(
            source_type=JournalEntry.Source.SALES, status=JournalEntry.Status.POSTED
        ).exists())

    def test_draft_delivery_invoices_reserve_quantity_and_edit_releases_reservation(self):
        delivery_id, line_id = self._create_delivery("DEL-DRAFT-RESERVATION")

        first = self.client.post("/api/sales-documents/invoices/", {
            "company": self.company.pk, "customer": self.customer.pk, "delivery": delivery_id,
            "invoice_number": "INV-DRAFT-RESERVE-1", "invoice_date": "2026-03-10",
            "lines": [{"delivery_line": line_id, "quantity": "1.5000", "unit_price": "100", "tax_rate": "0"}],
        }, format="json")
        self.assertEqual(first.status_code, 201, first.data)

        second = self.client.post("/api/sales-documents/invoices/", {
            "company": self.company.pk, "customer": self.customer.pk, "delivery": delivery_id,
            "invoice_number": "INV-DRAFT-RESERVE-2", "invoice_date": "2026-03-10",
            "lines": [{"delivery_line": line_id, "quantity": "0.5000", "unit_price": "100", "tax_rate": "0"}],
        }, format="json")
        self.assertEqual(second.status_code, 201, second.data)

        blocked = self.client.post("/api/sales-documents/invoices/", {
            "company": self.company.pk, "customer": self.customer.pk, "delivery": delivery_id,
            "invoice_number": "INV-DRAFT-RESERVE-3", "invoice_date": "2026-03-10",
            "lines": [{"delivery_line": line_id, "quantity": "0.1000", "unit_price": "100", "tax_rate": "0"}],
        }, format="json")
        self.assertEqual(blocked.status_code, 400, blocked.data)

        revised = self.client.patch(
            f"/api/sales-documents/invoices/{first.data['id']}/",
            {"lines": [{
                "delivery_line": line_id, "quantity": "1.0000",
                "unit_price": "100", "tax_rate": "0",
            }]},
            format="json",
        )
        self.assertEqual(revised.status_code, 200, revised.data)
        self.assertEqual(revised.data["invoice_lines"][0]["quantity"], "1.0000")

        released = self.client.post("/api/sales-documents/invoices/", {
            "company": self.company.pk, "customer": self.customer.pk, "delivery": delivery_id,
            "invoice_number": "INV-DRAFT-RESERVE-3", "invoice_date": "2026-03-10",
            "lines": [{"delivery_line": line_id, "quantity": "0.5000", "unit_price": "100", "tax_rate": "0"}],
        }, format="json")
        self.assertEqual(released.status_code, 201, released.data)

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


    def test_customer_receipt_date_cannot_precede_invoice_date(self):
        delivery_id, line_id = self._create_delivery("DEL-RECEIPT-DATE")
        invoice = self.client.post("/api/sales-documents/invoices/", {
            "company": self.company.pk, "customer": self.customer.pk, "delivery": delivery_id,
            "invoice_number": "INV-RECEIPT-DATE", "invoice_date": "2026-03-10",
            "lines": [{"delivery_line": line_id, "unit_price": "100", "tax_rate": "0"}],
        }, format="json")
        self.assertEqual(invoice.status_code, 201, invoice.data)
        invoice_id = invoice.data["id"]
        posted = self.client.post(
            f"/api/sales-documents/invoices/{invoice_id}/post/", {}, format="json"
        )
        self.assertEqual(posted.status_code, 200, posted.data)

        receipt = self.client.post("/api/sales-documents/receipts/", {
            "company": self.company.pk, "customer": self.customer.pk, "invoice": invoice_id,
            "receipt_number": "RC-BEFORE-INVOICE", "receipt_date": "2026-03-09",
            "method": "bank", "amount": "50",
        }, format="json")
        self.assertEqual(receipt.status_code, 400, receipt.data)
        self.assertIn("receipt_date", receipt.data)

    def _create_posted_invoice(self, number, invoice_date):
        delivery_id, line_id = self._create_delivery(f"DEL-{number}", delivery_date=invoice_date)
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

        self._create_posted_invoice("AGE-0", "2026-06-25")
        one_to_thirty = self._create_posted_invoice("AGE-1", "2026-06-10")
        self._create_posted_invoice("AGE-2", "2026-05-20")
        self._create_posted_invoice("AGE-3", "2026-04-20")
        self._create_posted_invoice("AGE-4", "2026-03-01")
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



    def test_vat_report_groups_posted_sales_by_tax_code_and_category(self):
        code = TaxCode.objects.create(
            company=self.company, country="EG", code="VAT-STD-14", name_ar="ضريبة قياسية",
            category=TaxCode.Category.STANDARD, rate=Decimal("14"),
            effective_from=date(2026, 1, 1), input_tax_recoverable=True,
        )
        _, line_id = self._create_delivery("DEL-VAT-REPORT")
        invoice = self.client.post("/api/sales-documents/invoices/", {
            "company": self.company.pk, "customer": self.customer.pk,
            "delivery": self.company.sales_deliveries.get(delivery_number="DEL-VAT-REPORT").pk,
            "invoice_number": "INV-VAT-REPORT", "invoice_date": "2026-03-10",
            "lines": [{"delivery_line": line_id, "unit_price": "100", "tax_code": code.pk}],
        }, format="json")
        self.assertEqual(invoice.status_code, 201, invoice.data)
        posted = self.client.post(f"/api/sales-documents/invoices/{invoice.data['id']}/post/", {}, format="json")
        self.assertEqual(posted.status_code, 200, posted.data)

        report = self.client.get(
            f"/api/accounting/journal-entries/vat-return/?company={self.company.pk}&date_from=2026-03-01&date_to=2026-03-31"
        )
        self.assertEqual(report.status_code, 200, report.data)
        self.assertEqual(report.data["output_tax"], "28.0000")
        self.assertEqual(report.data["sales"]["by_code"][0]["tax_code"], "VAT-STD-14")
        self.assertEqual(report.data["sales"]["by_category"][0]["category"], "standard")

    def test_sales_vat_return_separates_standard_zero_exempt_and_out_of_scope_categories(self):
        service = Product.objects.create(
            company=self.company, unit=self.product.unit, code="SRV-VAT-CATEGORIES",
            name_ar="خدمة لاختبار التصنيفات الضريبية",
            product_type=Product.ProductType.SERVICE, stock_class=None,
        )
        codes = []
        for code, category, rate in (
            ("VAT-CAT-STD", TaxCode.Category.STANDARD, "14"),
            ("VAT-CAT-ZERO", TaxCode.Category.ZERO_RATED, "0"),
            ("VAT-CAT-EXEMPT", TaxCode.Category.EXEMPT, "0"),
            ("VAT-CAT-OOS", TaxCode.Category.OUT_OF_SCOPE, "0"),
        ):
            codes.append(TaxCode.objects.create(
                company=self.company, country="EG", code=code, name_ar=code,
                category=category, rate=Decimal(rate),
                effective_from=date(2026, 1, 1),
            ))

        invoice = self.client.post("/api/sales-documents/invoices/", {
            "company": self.company.pk, "customer": self.customer.pk,
            "branch": self.branch.pk, "invoice_number": "INV-VAT-CATEGORIES",
            "invoice_date": "2026-03-10",
            "lines": [
                {"product": service.pk, "quantity": "1", "unit_price": "100", "tax_code": code.pk}
                for code in codes
            ],
        }, format="json")
        self.assertEqual(invoice.status_code, 201, invoice.data)
        self.assertEqual(invoice.data["net_total"], "400.0000")
        self.assertEqual(invoice.data["tax_total"], "14.0000")
        posted = self.client.post(
            f"/api/sales-documents/invoices/{invoice.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(posted.status_code, 200, posted.data)

        report = self.client.get(
            f"/api/accounting/journal-entries/vat-return/?company={self.company.pk}"
            "&date_from=2026-03-01&date_to=2026-03-31"
        )
        self.assertEqual(report.status_code, 200, report.data)
        self.assertEqual(report.data["sales"]["taxable_base"], "400.0000")
        self.assertEqual(report.data["output_tax"], "14.0000")
        by_category = {row["category"]: row for row in report.data["sales"]["by_category"]}
        self.assertEqual(set(by_category), {"standard", "zero_rated", "exempt", "out_of_scope"})
        self.assertEqual(by_category["standard"]["taxable_base"], "100.0000")
        self.assertEqual(by_category["standard"]["tax_amount"], "14.0000")
        for category in ("zero_rated", "exempt", "out_of_scope"):
            self.assertEqual(by_category[category]["taxable_base"], "100.0000")
            self.assertEqual(Decimal(by_category[category]["tax_amount"]), Decimal("0.0000"))

        reverse_charge = TaxCode.objects.create(
            company=self.company, country="EG", code="VAT-CAT-RC", name_ar="احتساب عكسي",
            category=TaxCode.Category.REVERSE_CHARGE, rate=Decimal("14"),
            effective_from=date(2026, 1, 1),
        )
        rejected = self.client.post("/api/sales-documents/invoices/", {
            "company": self.company.pk, "customer": self.customer.pk,
            "branch": self.branch.pk, "invoice_number": "INV-VAT-REVERSE-CHARGE",
            "invoice_date": "2026-03-10",
            "lines": [{"product": service.pk, "quantity": "1", "unit_price": "100",
                       "tax_code": reverse_charge.pk}],
        }, format="json")
        self.assertEqual(rejected.status_code, 400, rejected.data)


    def test_vat_return_preserves_sales_return_and_reversal_in_their_posting_periods(self):
        code = TaxCode.objects.create(
            company=self.company, country="EG", code="VAT-REV-14", name_ar="ضريبة قياسية",
            category=TaxCode.Category.STANDARD, rate=Decimal("14"),
            effective_from=date(2026, 1, 1), input_tax_recoverable=True,
        )
        delivery_id, delivery_line_id = self._create_delivery("DEL-VAT-REVERSAL")
        invoice = self.client.post("/api/sales-documents/invoices/", {
            "company": self.company.pk, "customer": self.customer.pk, "delivery": delivery_id,
            "invoice_number": "INV-VAT-REVERSAL", "invoice_date": "2026-03-10",
            "lines": [{"delivery_line": delivery_line_id, "unit_price": "100", "tax_code": code.pk}],
        }, format="json")
        self.assertEqual(invoice.status_code, 201, invoice.data)
        posted_invoice = self.client.post(
            f"/api/sales-documents/invoices/{invoice.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(posted_invoice.status_code, 200, posted_invoice.data)

        sales_return = self.client.post("/api/sales-documents/returns/", {
            "company": self.company.pk, "invoice": invoice.data["id"],
            "return_number": "RET-VAT-REVERSAL", "return_date": "2026-03-12",
            "lines": [{"invoice_line": invoice.data["invoice_lines"][0]["id"], "quantity": "1"}],
        }, format="json")
        self.assertEqual(sales_return.status_code, 201, sales_return.data)
        posted_return = self.client.post(
            f"/api/sales-documents/returns/{sales_return.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(posted_return.status_code, 200, posted_return.data)
        reversed_return = self.client.post(
            f"/api/sales-documents/returns/{sales_return.data['id']}/reverse/",
            {"entry_date": "2026-03-14"}, format="json",
        )
        self.assertEqual(reversed_return.status_code, 200, reversed_return.data)

        def vat_report(start, end):
            response = self.client.get(
                f"/api/accounting/journal-entries/vat-return/?company={self.company.pk}"
                f"&date_from={start}&date_to={end}"
            )
            self.assertEqual(response.status_code, 200, response.data)
            return response.data

        # The original return continues to reduce output tax in its original period.
        original_period = vat_report("2026-03-10", "2026-03-12")
        self.assertEqual(original_period["output_tax"], "14.0000")
        self.assertEqual(original_period["sales"]["taxable_base"], "100.0000")

        # A return-only period reports the negative taxable base and output VAT.
        return_period = vat_report("2026-03-12", "2026-03-12")
        self.assertEqual(return_period["output_tax"], "-14.0000")
        self.assertEqual(return_period["sales"]["taxable_base"], "-100.0000")

        # The posted reversal restores the VAT in the period in which it was posted.
        reversal_period = vat_report("2026-03-14", "2026-03-14")
        self.assertEqual(reversal_period["output_tax"], "14.0000")
        self.assertEqual(reversal_period["sales"]["taxable_base"], "100.0000")

        # Across both periods, return and reversal cancel without changing original sales VAT.
        combined_period = vat_report("2026-03-10", "2026-03-14")
        self.assertEqual(combined_period["output_tax"], "28.0000")
        self.assertEqual(combined_period["sales"]["taxable_base"], "200.0000")


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

    def test_sales_invoice_tax_rounds_to_base_currency_minor_units(self):
        delivery_id, line_id = self._create_delivery("DEL-TAX-ROUND")
        code = TaxCode.objects.create(
            company=self.company, country="EG", code="VAT-ROUND", name_ar="ضريبة تقريب",
            category=TaxCode.Category.STANDARD, rate=Decimal("14"),
            effective_from=date(2026, 1, 1),
        )
        invoice = self.client.post("/api/sales-documents/invoices/", {
            "company": self.company.pk, "customer": self.customer.pk, "delivery": delivery_id,
            "invoice_number": "INV-TAX-ROUND", "invoice_date": "2026-03-10",
            "lines": [{"delivery_line": line_id, "unit_price": "0.3333", "tax_code": code.pk}],
        }, format="json")
        self.assertEqual(invoice.status_code, 201, invoice.data)
        self.assertEqual(invoice.data["invoice_lines"][0]["tax_amount"], "0.0900")
        self.assertEqual(invoice.data["tax_total"], "0.0900")

    def test_used_tax_code_rate_is_immutable_but_can_be_deactivated(self):
        delivery_id, line_id = self._create_delivery("DEL-TAX-LOCK")
        code = TaxCode.objects.create(
            company=self.company, country="EG", code="VAT-LOCK", name_ar="ضريبة ثابتة",
            category=TaxCode.Category.STANDARD, rate=Decimal("14"),
            effective_from=date(2026, 1, 1),
        )
        invoice = self.client.post("/api/sales-documents/invoices/", {
            "company": self.company.pk, "customer": self.customer.pk, "delivery": delivery_id,
            "invoice_number": "INV-TAX-LOCK", "invoice_date": "2026-03-10",
            "lines": [{"delivery_line": line_id, "unit_price": "100", "tax_code": code.pk}],
        }, format="json")
        self.assertEqual(invoice.status_code, 201, invoice.data)
        changed = self.client.patch(f"/api/accounting/tax-codes/{code.pk}/", {"rate": "15"}, format="json")
        self.assertEqual(changed.status_code, 400)
        disabled = self.client.patch(f"/api/accounting/tax-codes/{code.pk}/", {"is_active": False}, format="json")
        self.assertEqual(disabled.status_code, 200, disabled.data)
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


    def test_discount_above_company_limit_requires_separate_manager_approval(self):
        DiscountApprovalPolicy.objects.create(
            company=self.company, max_discount_percent=Decimal("5"),
            created_by=self.user, updated_by=self.user,
        )
        delivery_id, line_id = self._create_delivery("DEL-DISCOUNT-APPROVAL")
        invoice = self.client.post("/api/sales-documents/invoices/", {
            "company": self.company.pk, "customer": self.customer.pk, "delivery": delivery_id,
            "invoice_number": "INV-DISCOUNT-APPROVAL", "invoice_date": "2026-03-10",
            "lines": [{
                "delivery_line": line_id, "unit_price": "100", "tax_rate": "14",
                "discount_percent": "10",
            }],
        }, format="json")
        self.assertEqual(invoice.status_code, 201, invoice.data)
        self.assertEqual(invoice.data["discount_total"], "20.0000")
        self.assertEqual(invoice.data["net_total"], "180.0000")
        self.assertEqual(invoice.data["tax_total"], "25.2000")
        self.assertEqual(invoice.data["approval_status"], "pending")
        invoice_id = invoice.data["id"]

        blocked = self.client.post(f"/api/sales-documents/invoices/{invoice_id}/post/", {}, format="json")
        self.assertEqual(blocked.status_code, 400, blocked.data)
        approval = SalesInvoiceApproval.objects.get(invoice_id=invoice_id)
        self.assertEqual(approval.status, SalesInvoiceApproval.Status.PENDING)

        self_approval = self.client.post(
            f"/api/approvals/requests/{approval.pk}/approve/", {"note": "محاولة اعتماد ذاتي"}, format="json"
        )
        self.assertEqual(self_approval.status_code, 403, self_approval.data)

        manager = get_user_model().objects.create_user(username="sales-manager", password="test-password")
        CompanyMembership.objects.create(
            user=manager, company=self.company, role="admin", all_branches=True,
        )
        self.client.force_authenticate(manager)
        approved = self.client.post(
            f"/api/approvals/requests/{approval.pk}/approve/", {"note": "موافقة المدير"}, format="json"
        )
        self.assertEqual(approved.status_code, 200, approved.data)
        self.assertEqual(approved.data["status"], "approved")

        self.client.force_authenticate(self.user)
        posted = self.client.post(f"/api/sales-documents/invoices/{invoice_id}/post/", {}, format="json")
        self.assertEqual(posted.status_code, 200, posted.data)
        self.assertEqual(posted.data["status"], "posted")
        self.assertEqual(posted.data["approval_status"], "approved")

    def test_discount_within_company_limit_posts_without_approval(self):
        DiscountApprovalPolicy.objects.create(
            company=self.company, max_discount_percent=Decimal("10"),
            created_by=self.user, updated_by=self.user,
        )
        delivery_id, line_id = self._create_delivery("DEL-DISCOUNT-WITHIN")
        invoice = self.client.post("/api/sales-documents/invoices/", {
            "company": self.company.pk, "customer": self.customer.pk, "delivery": delivery_id,
            "invoice_number": "INV-DISCOUNT-WITHIN", "invoice_date": "2026-03-10",
            "lines": [{
                "delivery_line": line_id, "unit_price": "100", "tax_rate": "0",
                "discount_percent": "7.5",
            }],
        }, format="json")
        self.assertEqual(invoice.status_code, 201, invoice.data)
        self.assertEqual(invoice.data["discount_total"], "15.0000")
        self.assertEqual(invoice.data["approval_status"], "not_required")
        posted = self.client.post(f"/api/sales-documents/invoices/{invoice.data['id']}/post/", {}, format="json")
        self.assertEqual(posted.status_code, 200, posted.data)

    def test_rejected_discount_request_cannot_be_posted(self):
        DiscountApprovalPolicy.objects.create(
            company=self.company, max_discount_percent=Decimal("0"),
            created_by=self.user, updated_by=self.user,
        )
        delivery_id, line_id = self._create_delivery("DEL-DISCOUNT-REJECT")
        invoice = self.client.post("/api/sales-documents/invoices/", {
            "company": self.company.pk, "customer": self.customer.pk, "delivery": delivery_id,
            "invoice_number": "INV-DISCOUNT-REJECT", "invoice_date": "2026-03-10",
            "lines": [{"delivery_line": line_id, "unit_price": "100", "discount_percent": "1"}],
        }, format="json")
        self.assertEqual(invoice.status_code, 201, invoice.data)
        approval = SalesInvoiceApproval.objects.get(invoice_id=invoice.data["id"])
        manager = get_user_model().objects.create_user(username="rejecting-manager", password="test-password")
        CompanyMembership.objects.create(user=manager, company=self.company, role="admin", all_branches=True)
        self.client.force_authenticate(manager)
        rejected = self.client.post(
            f"/api/approvals/requests/{approval.pk}/reject/", {"note": "الخصم غير مبرر"}, format="json"
        )
        self.assertEqual(rejected.status_code, 200, rejected.data)
        self.assertEqual(rejected.data["status"], "rejected")
        posted = self.client.post(f"/api/sales-documents/invoices/{invoice.data['id']}/post/", {}, format="json")
        self.assertEqual(posted.status_code, 400, posted.data)


    def test_approval_request_identity_and_history_are_protected_in_postgresql(self):
        DiscountApprovalPolicy.objects.create(
            company=self.company, max_discount_percent=Decimal("0"),
            created_by=self.user, updated_by=self.user,
        )
        delivery_id, line_id = self._create_delivery("DEL-DISCOUNT-IMMUTABLE")
        invoice = self.client.post("/api/sales-documents/invoices/", {
            "company": self.company.pk, "customer": self.customer.pk, "delivery": delivery_id,
            "invoice_number": "INV-DISCOUNT-IMMUTABLE", "invoice_date": "2026-03-10",
            "lines": [{"delivery_line": line_id, "unit_price": "100", "discount_percent": "1"}],
        }, format="json")
        self.assertEqual(invoice.status_code, 201, invoice.data)
        approval = SalesInvoiceApproval.objects.get(invoice_id=invoice.data["id"])
        with self.assertRaises(DatabaseError):
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(
                        "UPDATE approvals_salesinvoiceapproval SET requested_limit = %s WHERE id = %s",
                        ["99", approval.pk],
                    )
        with self.assertRaises(DatabaseError):
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(
                        "UPDATE approvals_salesinvoiceapproval SET requested_lines_snapshot = %s::jsonb WHERE id = %s",
                        ["[]", approval.pk],
                    )
        with self.assertRaises(DatabaseError):
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("DELETE FROM approvals_salesinvoiceapproval WHERE id = %s", [approval.pk])
        approval.refresh_from_db()
        self.assertEqual(approval.requested_limit, Decimal("0.0000"))
        self.assertEqual(approval.status, SalesInvoiceApproval.Status.PENDING)


    def test_discount_policy_can_only_be_managed_by_company_admin(self):
        manager = get_user_model().objects.create_user(username="policy-admin", password="test-password")
        CompanyMembership.objects.create(user=manager, company=self.company, role="admin", all_branches=True)
        self.client.force_authenticate(self.user)
        denied = self.client.post("/api/approvals/discount-policies/", {
            "company": self.company.pk, "max_discount_percent": "5",
        }, format="json")
        self.assertEqual(denied.status_code, 403, denied.data)

        self.client.force_authenticate(manager)
        created = self.client.post("/api/approvals/discount-policies/", {
            "company": self.company.pk, "max_discount_percent": "5",
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        updated = self.client.patch(
            f"/api/approvals/discount-policies/{created.data['id']}/",
            {"max_discount_percent": "7.5"}, format="json",
        )
        self.assertEqual(updated.status_code, 200, updated.data)
        self.assertEqual(updated.data["max_discount_percent"], "7.5000")

    def test_invoice_creator_who_is_company_admin_cannot_approve_own_discount_request(self):
        manager = get_user_model().objects.create_user(username="self-approval-manager", password="test-password")
        CompanyMembership.objects.create(user=manager, company=self.company, role="admin", all_branches=True)
        DiscountApprovalPolicy.objects.create(
            company=self.company, max_discount_percent=Decimal("0"),
            created_by=manager, updated_by=manager,
        )
        self.client.force_authenticate(manager)
        delivery_id, line_id = self._create_delivery("DEL-SELF-APPROVAL")
        invoice = self.client.post("/api/sales-documents/invoices/", {
            "company": self.company.pk, "customer": self.customer.pk, "delivery": delivery_id,
            "invoice_number": "INV-SELF-APPROVAL", "invoice_date": "2026-03-10",
            "lines": [{"delivery_line": line_id, "unit_price": "100", "discount_percent": "1"}],
        }, format="json")
        self.assertEqual(invoice.status_code, 201, invoice.data)
        approval = SalesInvoiceApproval.objects.get(invoice_id=invoice.data["id"])
        response = self.client.post(
            f"/api/approvals/requests/{approval.pk}/approve/", {"note": "اعتماد ذاتي"}, format="json"
        )
        self.assertEqual(response.status_code, 403, response.data)


    def test_rejected_discount_can_be_revised_and_resubmitted_without_losing_history(self):
        DiscountApprovalPolicy.objects.create(
            company=self.company, max_discount_percent=Decimal("5"),
            created_by=self.user, updated_by=self.user,
        )
        delivery_id, line_id = self._create_delivery("DEL-DISCOUNT-REVISE")
        invoice = self.client.post("/api/sales-documents/invoices/", {
            "company": self.company.pk, "customer": self.customer.pk, "delivery": delivery_id,
            "invoice_number": "INV-DISCOUNT-REVISE", "invoice_date": "2026-03-10",
            "lines": [{"delivery_line": line_id, "unit_price": "100", "discount_percent": "10"}],
        }, format="json")
        self.assertEqual(invoice.status_code, 201, invoice.data)
        first = SalesInvoiceApproval.objects.get(invoice_id=invoice.data["id"])

        manager = get_user_model().objects.create_user(username="revision-manager", password="test-password")
        CompanyMembership.objects.create(user=manager, company=self.company, role="admin", all_branches=True)
        self.client.force_authenticate(manager)
        rejected = self.client.post(
            f"/api/approvals/requests/{first.pk}/reject/", {"note": "الخصم مرتفع"}, format="json"
        )
        self.assertEqual(rejected.status_code, 200, rejected.data)

        self.client.force_authenticate(self.user)
        revised = self.client.patch(
            f"/api/sales-documents/invoices/{invoice.data['id']}/",
            {"lines": [{"delivery_line": line_id, "unit_price": "100", "discount_percent": "8"}]},
            format="json",
        )
        self.assertEqual(revised.status_code, 200, revised.data)
        self.assertEqual(revised.data["approval_status"], "pending")
        self.assertEqual(revised.data["discount_total"], "16.0000")
        self.assertEqual(SalesInvoiceApproval.objects.filter(invoice_id=invoice.data["id"]).count(), 2)
        first.refresh_from_db()
        second = SalesInvoiceApproval.objects.order_by("-requested_at", "-pk").first()
        self.assertEqual(first.status, SalesInvoiceApproval.Status.REJECTED)
        self.assertEqual(second.status, SalesInvoiceApproval.Status.PENDING)
        history = self.client.get(f"/api/approvals/requests/?company={self.company.pk}")
        self.assertEqual(history.status_code, 200, history.data)
        first_view = next(row for row in history.data if row["id"] == first.pk)
        second_view = next(row for row in history.data if row["id"] == second.pk)
        self.assertEqual(first_view["invoice_total"], "180.0000")
        self.assertEqual(second_view["invoice_total"], "184.0000")

        self.client.force_authenticate(manager)
        approved = self.client.post(
            f"/api/approvals/requests/{second.pk}/approve/", {"note": "موافقة بعد المراجعة"}, format="json"
        )
        self.assertEqual(approved.status_code, 200, approved.data)
        self.client.force_authenticate(self.user)
        posted = self.client.post(f"/api/sales-documents/invoices/{invoice.data['id']}/post/", {}, format="json")
        self.assertEqual(posted.status_code, 200, posted.data)

    def test_revising_invoice_below_limit_supersedes_pending_request(self):
        DiscountApprovalPolicy.objects.create(
            company=self.company, max_discount_percent=Decimal("5"),
            created_by=self.user, updated_by=self.user,
        )
        delivery_id, line_id = self._create_delivery("DEL-DISCOUNT-SUPERSEDE")
        invoice = self.client.post("/api/sales-documents/invoices/", {
            "company": self.company.pk, "customer": self.customer.pk, "delivery": delivery_id,
            "invoice_number": "INV-DISCOUNT-SUPERSEDE", "invoice_date": "2026-03-10",
            "lines": [{"delivery_line": line_id, "unit_price": "100", "discount_percent": "10"}],
        }, format="json")
        self.assertEqual(invoice.status_code, 201, invoice.data)
        approval = SalesInvoiceApproval.objects.get(invoice_id=invoice.data["id"])
        revised = self.client.patch(
            f"/api/sales-documents/invoices/{invoice.data['id']}/",
            {"lines": [{"delivery_line": line_id, "unit_price": "100", "discount_percent": "2"}]},
            format="json",
        )
        self.assertEqual(revised.status_code, 200, revised.data)
        self.assertEqual(revised.data["approval_status"], "not_required")
        approval.refresh_from_db()
        self.assertEqual(approval.status, SalesInvoiceApproval.Status.SUPERSEDED)
        posted = self.client.post(f"/api/sales-documents/invoices/{invoice.data['id']}/post/", {}, format="json")
        self.assertEqual(posted.status_code, 200, posted.data)


    def test_discount_policy_cannot_be_deleted_directly_from_postgresql(self):
        policy = DiscountApprovalPolicy.objects.create(
            company=self.company, max_discount_percent=Decimal("5"),
            created_by=self.user, updated_by=self.user,
        )
        with self.assertRaises(DatabaseError):
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(
                        "DELETE FROM approvals_discountapprovalpolicy WHERE id = %s",
                        [policy.pk],
                    )
        self.assertTrue(DiscountApprovalPolicy.objects.filter(pk=policy.pk).exists())


    def test_changing_discount_after_approval_requires_a_new_decision(self):
        DiscountApprovalPolicy.objects.create(
            company=self.company, max_discount_percent=Decimal("5"),
            created_by=self.user, updated_by=self.user,
        )
        delivery_id, line_id = self._create_delivery("DEL-DISCOUNT-REAPPROVE")
        invoice = self.client.post("/api/sales-documents/invoices/", {
            "company": self.company.pk, "customer": self.customer.pk, "delivery": delivery_id,
            "invoice_number": "INV-DISCOUNT-REAPPROVE", "invoice_date": "2026-03-10",
            "lines": [{"delivery_line": line_id, "unit_price": "100", "discount_percent": "10"}],
        }, format="json")
        self.assertEqual(invoice.status_code, 201, invoice.data)
        manager = get_user_model().objects.create_user(username="reapproval-manager", password="test-password")
        CompanyMembership.objects.create(user=manager, company=self.company, role="admin", all_branches=True)
        first = SalesInvoiceApproval.objects.get(invoice_id=invoice.data["id"])
        self.client.force_authenticate(manager)
        approved_first = self.client.post(
            f"/api/approvals/requests/{first.pk}/approve/", {"note": "اعتماد أول"}, format="json"
        )
        self.assertEqual(approved_first.status_code, 200, approved_first.data)

        self.client.force_authenticate(self.user)
        revised = self.client.patch(
            f"/api/sales-documents/invoices/{invoice.data['id']}/",
            {"lines": [{"delivery_line": line_id, "unit_price": "100", "discount_percent": "12"}]},
            format="json",
        )
        self.assertEqual(revised.status_code, 200, revised.data)
        self.assertEqual(revised.data["approval_status"], "pending")
        first.refresh_from_db()
        second = SalesInvoiceApproval.objects.order_by("-requested_at", "-pk").first()
        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual(first.status, SalesInvoiceApproval.Status.APPROVED)
        blocked = self.client.post(f"/api/sales-documents/invoices/{invoice.data['id']}/post/", {}, format="json")
        self.assertEqual(blocked.status_code, 400, blocked.data)

        self.client.force_authenticate(manager)
        approved_second = self.client.post(
            f"/api/approvals/requests/{second.pk}/approve/", {"note": "اعتماد بعد تعديل الخصم"}, format="json"
        )
        self.assertEqual(approved_second.status_code, 200, approved_second.data)
        self.client.force_authenticate(self.user)
        posted = self.client.post(f"/api/sales-documents/invoices/{invoice.data['id']}/post/", {}, format="json")
        self.assertEqual(posted.status_code, 200, posted.data)



    def test_sales_invoice_above_company_amount_limit_requires_manager_approval(self):
        DiscountApprovalPolicy.objects.create(
            company=self.company, max_discount_percent=None,
            max_sales_invoice_amount=Decimal("200"),
            created_by=self.user, updated_by=self.user,
        )
        delivery_id, line_id = self._create_delivery("DEL-AMOUNT-APPROVAL")
        invoice = self.client.post("/api/sales-documents/invoices/", {
            "company": self.company.pk, "customer": self.customer.pk, "delivery": delivery_id,
            "invoice_number": "INV-AMOUNT-APPROVAL", "invoice_date": "2026-03-10",
            "lines": [{"delivery_line": line_id, "unit_price": "100", "tax_rate": "14"}],
        }, format="json")
        self.assertEqual(invoice.status_code, 201, invoice.data)
        self.assertEqual(invoice.data["grand_total"], "228.0000")
        self.assertEqual(invoice.data["invoice_approval_limit"], "200.0000")
        self.assertEqual(invoice.data["approval_status"], "pending")
        approval = SalesInvoiceApproval.objects.get(invoice_id=invoice.data["id"])
        self.assertIsNone(approval.requested_limit)
        self.assertEqual(approval.requested_invoice_limit, Decimal("200.0000"))
        blocked = self.client.post(
            f"/api/sales-documents/invoices/{invoice.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(blocked.status_code, 400, blocked.data)

        manager = get_user_model().objects.create_user(username="amount-limit-manager", password="test-password")
        CompanyMembership.objects.create(user=manager, company=self.company, role="admin", all_branches=True)
        self.client.force_authenticate(manager)
        approved = self.client.post(
            f"/api/approvals/requests/{approval.pk}/approve/", {"note": "اعتماد قيمة الفاتورة"}, format="json"
        )
        self.assertEqual(approved.status_code, 200, approved.data)
        self.client.force_authenticate(self.user)
        posted = self.client.post(
            f"/api/sales-documents/invoices/{invoice.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(posted.status_code, 200, posted.data)
        self.assertEqual(posted.data["status"], "posted")

    def test_sales_invoice_at_amount_limit_does_not_require_approval(self):
        DiscountApprovalPolicy.objects.create(
            company=self.company, max_discount_percent=Decimal("100"),
            max_sales_invoice_amount=Decimal("228"),
            created_by=self.user, updated_by=self.user,
        )
        delivery_id, line_id = self._create_delivery("DEL-AMOUNT-BOUNDARY")
        invoice = self.client.post("/api/sales-documents/invoices/", {
            "company": self.company.pk, "customer": self.customer.pk, "delivery": delivery_id,
            "invoice_number": "INV-AMOUNT-BOUNDARY", "invoice_date": "2026-03-10",
            "lines": [{"delivery_line": line_id, "unit_price": "100", "tax_rate": "14"}],
        }, format="json")
        self.assertEqual(invoice.status_code, 201, invoice.data)
        self.assertEqual(invoice.data["approval_status"], "not_required")
        posted = self.client.post(
            f"/api/sales-documents/invoices/{invoice.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(posted.status_code, 200, posted.data)

    def test_revising_amount_limited_invoice_below_limit_supersedes_pending_request(self):
        DiscountApprovalPolicy.objects.create(
            company=self.company, max_discount_percent=Decimal("100"),
            max_sales_invoice_amount=Decimal("200"),
            created_by=self.user, updated_by=self.user,
        )
        delivery_id, line_id = self._create_delivery("DEL-AMOUNT-REVISE")
        invoice = self.client.post("/api/sales-documents/invoices/", {
            "company": self.company.pk, "customer": self.customer.pk, "delivery": delivery_id,
            "invoice_number": "INV-AMOUNT-REVISE", "invoice_date": "2026-03-10",
            "lines": [{"delivery_line": line_id, "unit_price": "100", "tax_rate": "14"}],
        }, format="json")
        self.assertEqual(invoice.status_code, 201, invoice.data)
        approval = SalesInvoiceApproval.objects.get(invoice_id=invoice.data["id"])
        revised = self.client.patch(
            f"/api/sales-documents/invoices/{invoice.data['id']}/",
            {"lines": [{"delivery_line": line_id, "unit_price": "80", "tax_rate": "14"}]},
            format="json",
        )
        self.assertEqual(revised.status_code, 200, revised.data)
        self.assertEqual(revised.data["approval_status"], "not_required")
        approval.refresh_from_db()
        self.assertEqual(approval.status, SalesInvoiceApproval.Status.SUPERSEDED)
        posted = self.client.post(
            f"/api/sales-documents/invoices/{invoice.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(posted.status_code, 200, posted.data)


    def test_requested_invoice_amount_limit_is_immutable_in_postgresql(self):
        DiscountApprovalPolicy.objects.create(
            company=self.company, max_discount_percent=Decimal("100"),
            max_sales_invoice_amount=Decimal("200"),
            created_by=self.user, updated_by=self.user,
        )
        delivery_id, line_id = self._create_delivery("DEL-AMOUNT-IMMUTABLE")
        invoice = self.client.post("/api/sales-documents/invoices/", {
            "company": self.company.pk, "customer": self.customer.pk, "delivery": delivery_id,
            "invoice_number": "INV-AMOUNT-IMMUTABLE", "invoice_date": "2026-03-10",
            "lines": [{"delivery_line": line_id, "unit_price": "100", "tax_rate": "14"}],
        }, format="json")
        self.assertEqual(invoice.status_code, 201, invoice.data)
        approval = SalesInvoiceApproval.objects.get(invoice_id=invoice.data["id"])
        with self.assertRaises(DatabaseError):
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(
                        "UPDATE approvals_salesinvoiceapproval SET requested_invoice_limit = %s WHERE id = %s",
                        ["999", approval.pk],
                    )
        approval.refresh_from_db()
        self.assertEqual(approval.requested_invoice_limit, Decimal("200.0000"))


    def test_company_admin_can_create_amount_only_approval_policy(self):
        manager = get_user_model().objects.create_user(username="amount-policy-admin", password="test-password")
        CompanyMembership.objects.create(user=manager, company=self.company, role="admin", all_branches=True)
        self.client.force_authenticate(manager)
        response = self.client.post("/api/approvals/discount-policies/", {
            "company": self.company.pk, "max_sales_invoice_amount": "5000",
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertIsNone(response.data["max_discount_percent"])
        self.assertEqual(response.data["max_sales_invoice_amount"], "5000.0000")

    def test_company_admin_cannot_create_empty_approval_policy(self):
        manager = get_user_model().objects.create_user(username="empty-policy-admin", password="test-password")
        CompanyMembership.objects.create(user=manager, company=self.company, role="admin", all_branches=True)
        self.client.force_authenticate(manager)
        response = self.client.post("/api/approvals/discount-policies/", {
            "company": self.company.pk,
        }, format="json")
        self.assertEqual(response.status_code, 400, response.data)

    def test_database_rejects_approval_policy_without_any_threshold(self):
        with self.assertRaises(DatabaseError):
            with transaction.atomic():
                DiscountApprovalPolicy.objects.create(
                    company=self.company, max_discount_percent=None,
                    max_sales_invoice_amount=None, created_by=self.user, updated_by=self.user,
                )
        self.assertFalse(DiscountApprovalPolicy.objects.filter(company=self.company).exists())


    def test_customer_statement_reports_posted_invoice_and_opening_balance(self):
        delivery_id, line_id = self._create_delivery("DEL-CUSTOMER-STATEMENT")
        invoice = self.client.post("/api/sales-documents/invoices/", {
            "company": self.company.pk, "customer": self.customer.pk, "delivery": delivery_id,
            "invoice_number": "INV-CUSTOMER-STATEMENT", "invoice_date": "2026-03-10",
            "lines": [{"delivery_line": line_id, "unit_price": "100", "tax_rate": "14"}],
        }, format="json")
        self.assertEqual(invoice.status_code, 201, invoice.data)
        posted = self.client.post(
            f"/api/sales-documents/invoices/{invoice.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(posted.status_code, 200, posted.data)

        full_period = self.client.get(
            f"/api/sales-documents/invoices/customer-statement/?company={self.company.pk}"
            f"&customer={self.customer.pk}&date_from=2026-03-01&date_to=2026-03-31"
        )
        self.assertEqual(full_period.status_code, 200, full_period.data)
        self.assertEqual(full_period.data["customer_name"], self.customer.name_ar)
        self.assertEqual(len(full_period.data["rows"]), 1)
        self.assertEqual(full_period.data["rows"][0]["debit"], "228.0000")
        self.assertEqual(full_period.data["closing_balance"], "228.0000")

        next_period = self.client.get(
            f"/api/sales-documents/invoices/customer-statement/?company={self.company.pk}"
            f"&customer={self.customer.pk}&date_from=2026-03-11&date_to=2026-03-31"
        )
        self.assertEqual(next_period.status_code, 200, next_period.data)
        self.assertEqual(next_period.data["opening_balance"], "228.0000")
        self.assertEqual(next_period.data["closing_balance"], "228.0000")
        self.assertEqual(next_period.data["rows"], [])

    def test_customer_statement_rejects_inaccessible_company(self):
        other_currency = Currency.objects.create(code="SAR", name_ar="ريال سعودي")
        other_company = Company.objects.create(
            code="OTHER-STATEMENT", name_ar="شركة أخرى", country="SA",
            activity="commercial", base_currency=other_currency,
        )
        response = self.client.get(
            f"/api/sales-documents/invoices/customer-statement/?company={other_company.pk}"
            f"&customer={self.customer.pk}"
        )
        self.assertEqual(response.status_code, 403)



    def test_sales_return_posts_stock_cost_and_tax_and_blocks_over_return(self):
        delivery_id, delivery_line_id = self._create_delivery("DEL-RETURN-001")
        invoice = self.client.post("/api/sales-documents/invoices/", {
            "company": self.company.pk, "customer": self.customer.pk, "delivery": delivery_id,
            "invoice_number": "INV-RETURN-001", "invoice_date": "2026-03-10",
            "lines": [{"delivery_line": delivery_line_id, "unit_price": "100", "tax_rate": "14"}],
        }, format="json")
        self.assertEqual(invoice.status_code, 201, invoice.data)
        posted_invoice = self.client.post(
            f"/api/sales-documents/invoices/{invoice.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(posted_invoice.status_code, 200, posted_invoice.data)

        created = self.client.post("/api/sales-documents/returns/", {
            "company": self.company.pk, "invoice": invoice.data["id"],
            "return_number": "RET-001", "return_date": "2026-03-12",
            "lines": [{"invoice_line": invoice.data["invoice_lines"][0]["id"], "quantity": "1"}],
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        self.assertEqual(created.data["line_items"][0]["net_amount"], "100.0000")
        self.assertEqual(created.data["line_items"][0]["tax_amount"], "14.0000")
        self.assertEqual(created.data["line_items"][0]["cost_amount"], "40.0000")

        posted = self.client.post(
            f"/api/sales-documents/returns/{created.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(posted.status_code, 200, posted.data)
        self.assertEqual(posted.data["status"], "posted")
        self.assertEqual(posted.data["grand_total"], "114.0000")
        invoice_after_return = self.client.get(
            f"/api/sales-documents/invoices/{invoice.data['id']}/"
        )
        self.assertEqual(invoice_after_return.status_code, 200, invoice_after_return.data)
        self.assertEqual(invoice_after_return.data["balance_due"], "114.0000")
        balance = StockBalance.objects.get(product=self.product, warehouse=self.warehouse)
        self.assertEqual(balance.quantity, Decimal("4.0000"))
        self.assertEqual(balance.inventory_value, Decimal("160.0000"))
        cost_entry = JournalEntry.objects.get(pk=posted.data["cost_journal_entry"])
        self.assertEqual(
            sum((line.debit for line in cost_entry.lines.all()), Decimal("0")),
            Decimal("40.0000"),
        )
        self.assertEqual(
            sum((line.credit for line in cost_entry.lines.all()), Decimal("0")),
            Decimal("40.0000"),
        )

        excessive = self.client.post("/api/sales-documents/returns/", {
            "company": self.company.pk, "invoice": invoice.data["id"],
            "return_number": "RET-002", "return_date": "2026-03-13",
            "lines": [{"invoice_line": invoice.data["invoice_lines"][0]["id"], "quantity": "2"}],
        }, format="json")
        self.assertEqual(excessive.status_code, 201, excessive.data)
        rejected = self.client.post(
            f"/api/sales-documents/returns/{excessive.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(rejected.status_code, 400, rejected.data)
        self.assertEqual(StockBalance.objects.get(product=self.product, warehouse=self.warehouse).quantity, Decimal("4.0000"))

        reversed_return = self.client.post(
            f"/api/sales-documents/returns/{created.data['id']}/reverse/",
            {"entry_date": "2026-03-14"}, format="json",
        )
        self.assertEqual(reversed_return.status_code, 200, reversed_return.data)
        self.assertEqual(reversed_return.data["status"], "reversed")
        invoice_after_reversal = self.client.get(
            f"/api/sales-documents/invoices/{invoice.data['id']}/"
        )
        self.assertEqual(invoice_after_reversal.status_code, 200, invoice_after_reversal.data)
        self.assertEqual(invoice_after_reversal.data["balance_due"], "228.0000")
        self.assertIsNotNone(reversed_return.data["reversal_journal_entry"])
        self.assertIsNotNone(reversed_return.data["reversal_stock_movement"])
        balance.refresh_from_db()
        self.assertEqual(balance.quantity, Decimal("3.0000"))
        self.assertEqual(balance.inventory_value, Decimal("120.0000"))
        original_return_journal = JournalEntry.objects.get(pk=posted.data["journal_entry"])
        self.assertEqual(original_return_journal.status, JournalEntry.Status.REVERSED)
        reversal_movement = StockMovement.objects.get(pk=reversed_return.data["reversal_stock_movement"])
        self.assertEqual(reversal_movement.movement_type, StockMovement.MovementType.SALES_RETURN_REVERSAL)
        reversal_line = reversal_movement.lines.get()
        self.assertIsNotNone(reversal_line.reversal_of_line_id)
        self.assertEqual(reversal_line.reversal_of_line.movement.movement_type, StockMovement.MovementType.RECEIPT)
        consumed = sum(
            StockCostConsumption.objects.filter(movement_line=reversal_line).values_list("quantity", flat=True),
            Decimal("0"),
        )
        self.assertEqual(consumed, Decimal("1.0000"))
        repeated = self.client.post(
            f"/api/sales-documents/returns/{created.data['id']}/reverse/",
            {"entry_date": "2026-03-15"}, format="json",
        )
        self.assertEqual(repeated.status_code, 400, repeated.data)

    def test_foreign_customer_invoice_partial_and_final_receipts_post_realized_fx(self):
        delivery_id, line_id = self._create_delivery("DEL-FX-001")
        usd = Currency.objects.create(code="USD", name_ar="دولار أمريكي")
        ExchangeRate.objects.create(company=self.company, currency=usd, rate_date=date(2026, 3, 10), rate=Decimal("50"))
        ExchangeRate.objects.create(company=self.company, currency=usd, rate_date=date(2026, 3, 11), rate=Decimal("52"))
        ExchangeRate.objects.create(company=self.company, currency=usd, rate_date=date(2026, 3, 12), rate=Decimal("48"))

        created = self.client.post("/api/sales-documents/invoices/", {
            "company": self.company.pk, "customer": self.customer.pk, "delivery": delivery_id,
            "invoice_number": "INV-FX-001", "invoice_date": "2026-03-10",
            "transaction_currency": usd.pk, "exchange_rate": "50", "exchange_rate_date": "2026-03-10",
            "lines": [{"delivery_line": line_id, "transaction_unit_price": "100", "tax_rate": "0"}],
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        invoice_id = created.data["id"]
        posted = self.client.post(f"/api/sales-documents/invoices/{invoice_id}/post/", {}, format="json")
        self.assertEqual(posted.status_code, 200, posted.data)
        self.assertEqual(posted.data["grand_total"], "10000.0000")
        self.assertEqual(posted.data["transaction_grand_total"], "200.0000")

        first = self.client.post("/api/sales-documents/receipts/", {
            "company": self.company.pk, "customer": self.customer.pk, "invoice": invoice_id,
            "receipt_number": "RC-FX-001", "receipt_date": "2026-03-11", "method": "cash",
            "amount": "2080.0000", "transaction_currency": usd.pk, "transaction_amount": "40",
            "exchange_rate": "52", "exchange_rate_date": "2026-03-11",
        }, format="json")
        self.assertEqual(first.status_code, 201, first.data)
        self.assertEqual(first.data["transaction_currency_code"], "USD")
        first_posted = self.client.post(f"/api/sales-documents/receipts/{first.data['id']}/post/", {}, format="json")
        self.assertEqual(first_posted.status_code, 200, first_posted.data)
        self.assertEqual(first_posted.data["transaction_currency_code"], "USD")
        self.assertEqual(first_posted.data["carrying_base_amount"], "2000.0000")
        self.assertEqual(first_posted.data["realized_fx_gain"], "80.0000")
        gain_line = JournalLine.objects.get(entry_id=first_posted.data["journal_entry"], account__code="4900")
        self.assertEqual(gain_line.credit, Decimal("80.0000"))

        final = self.client.post("/api/sales-documents/receipts/", {
            "company": self.company.pk, "customer": self.customer.pk, "invoice": invoice_id,
            "receipt_number": "RC-FX-002", "receipt_date": "2026-03-12", "method": "cash",
            "amount": "7680.0000", "transaction_currency": usd.pk, "transaction_amount": "160",
            "exchange_rate": "48", "exchange_rate_date": "2026-03-12",
        }, format="json")
        self.assertEqual(final.status_code, 201, final.data)
        final_posted = self.client.post(f"/api/sales-documents/receipts/{final.data['id']}/post/", {}, format="json")
        self.assertEqual(final_posted.status_code, 200, final_posted.data)
        self.assertEqual(final_posted.data["carrying_base_amount"], "8000.0000")
        self.assertEqual(final_posted.data["realized_fx_loss"], "320.0000")
        loss_line = JournalLine.objects.get(entry_id=final_posted.data["journal_entry"], account__code="6900")
        self.assertEqual(loss_line.debit, Decimal("320.0000"))
        refreshed = self.client.get(f"/api/sales-documents/invoices/{invoice_id}/").data
        self.assertEqual(refreshed["paid_total"], "10000.0000")
        self.assertEqual(refreshed["balance_due"], "0.0000")

    def test_customer_aging_uses_fx_revalued_receivable_as_of_date(self):
        delivery_id, line_id = self._create_delivery("DEL-FX-AGING")
        usd = Currency.objects.create(code="USD", name_ar="دولار أمريكي")
        for rate_date, rate in (
            (date(2026, 3, 10), "50"),
            (date(2026, 3, 31), "53"),
        ):
            ExchangeRate.objects.create(
                company=self.company, currency=usd,
                rate_date=rate_date, rate=Decimal(rate),
            )
        invoice = self.client.post("/api/sales-documents/invoices/", {
            "company": self.company.pk, "customer": self.customer.pk,
            "delivery": delivery_id, "invoice_number": "INV-FX-AGING",
            "invoice_date": "2026-03-10",
            "transaction_currency": usd.pk, "exchange_rate": "50",
            "exchange_rate_date": "2026-03-10",
            "lines": [{"delivery_line": line_id, "transaction_unit_price": "100", "tax_rate": "0"}],
        }, format="json")
        self.assertEqual(invoice.status_code, 201, invoice.data)
        invoice_id = invoice.data["id"]
        posted = self.client.post(
            f"/api/sales-documents/invoices/{invoice_id}/post/", {}, format="json"
        )
        self.assertEqual(posted.status_code, 200, posted.data)

        before_revaluation = self.client.get(
            f"/api/sales-documents/invoices/customer-aging/?company={self.company.pk}&as_of=2026-03-30"
        )
        self.assertEqual(before_revaluation.status_code, 200, before_revaluation.data)
        before_row = next(
            row for customer in before_revaluation.data["customers"]
            for row in customer["invoices"] if row["invoice"] == invoice_id
        )
        self.assertEqual(before_row["balance_due"], "10000.0000")
        self.assertEqual(before_row["fx_revaluation_adjustment"], "0.0000")

        revalued = self.client.post(
            f"/api/sales-documents/invoices/{invoice_id}/revalue/",
            {"revaluation_date": "2026-03-31", "rate_date": "2026-03-31", "rate": "53"},
            format="json",
        )
        self.assertEqual(revalued.status_code, 201, revalued.data)

        after_revaluation = self.client.get(
            f"/api/sales-documents/invoices/customer-aging/?company={self.company.pk}&as_of=2026-03-31"
        )
        self.assertEqual(after_revaluation.status_code, 200, after_revaluation.data)
        after_row = next(
            row for customer in after_revaluation.data["customers"]
            for row in customer["invoices"] if row["invoice"] == invoice_id
        )
        self.assertEqual(after_row["balance_due"], "10600.0000")
        self.assertEqual(after_row["fx_revaluation_adjustment"], "600.0000")
        self.assertEqual(after_revaluation.data["totals"]["total_due"], "10600.0000")

    def test_fx_revaluation_journal_can_be_reversed_before_any_later_settlement(self):
        delivery_id, line_id = self._create_delivery("DEL-FX-REV")
        usd = Currency.objects.create(code="USD", name_ar="دولار أمريكي")
        for rate_date, rate in (
            (date(2026, 3, 10), "50"), (date(2026, 3, 31), "53"),
            (date(2026, 4, 2), "51"),
        ):
            ExchangeRate.objects.create(company=self.company, currency=usd, rate_date=rate_date, rate=Decimal(rate))
        created = self.client.post("/api/sales-documents/invoices/", {
            "company": self.company.pk, "customer": self.customer.pk, "delivery": delivery_id,
            "invoice_number": "INV-FX-REV", "invoice_date": "2026-03-10",
            "transaction_currency": usd.pk, "exchange_rate": "50", "exchange_rate_date": "2026-03-10",
            "lines": [{"delivery_line": line_id, "transaction_unit_price": "100", "tax_rate": "0"}],
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        invoice_id = created.data["id"]
        posted = self.client.post(f"/api/sales-documents/invoices/{invoice_id}/post/", {}, format="json")
        self.assertEqual(posted.status_code, 200, posted.data)
        revalued = self.client.post(f"/api/sales-documents/invoices/{invoice_id}/revalue/", {
            "revaluation_date": "2026-03-31", "rate_date": "2026-03-31", "rate": "53",
        }, format="json")
        self.assertEqual(revalued.status_code, 201, revalued.data)

        reversed_entry = self.client.post(
            f"/api/accounting/journal-entries/{revalued.data['journal_entry']}/reverse/",
            {"entry_date": "2026-04-01"}, format="json",
        )
        self.assertEqual(reversed_entry.status_code, 201, reversed_entry.data)
        history = self.client.get(f"/api/sales-documents/invoices/{invoice_id}/revaluations/")
        self.assertEqual(history.status_code, 200, history.data)
        self.assertEqual(history.data[0]["status"], "reversed")
        self.assertEqual(JournalEntry.objects.get(pk=revalued.data["journal_entry"]).status, JournalEntry.Status.REVERSED)

        later = self.client.post(f"/api/sales-documents/invoices/{invoice_id}/revalue/", {
            "revaluation_date": "2026-04-02", "rate_date": "2026-04-02", "rate": "51",
        }, format="json")
        self.assertEqual(later.status_code, 201, later.data)
        self.assertEqual(later.data["previous_carrying_amount"], "10000.0000")
        self.assertEqual(later.data["new_carrying_amount"], "10200.0000")

        # The reversed event remains in the audit history but must not affect
        # the carrying amount used by the next revaluation.
        history_after = self.client.get(f"/api/sales-documents/invoices/{invoice_id}/revaluations/")
        self.assertEqual(history_after.status_code, 200, history_after.data)
        self.assertEqual(len(history_after.data), 2)
        self.assertEqual(history_after.data[0]["status"], "posted")
        self.assertEqual(history_after.data[1]["status"], "reversed")
        self.assertEqual(history_after.data[1]["previous_carrying_amount"], "10000.0000")
        self.assertEqual(history_after.data[1]["new_carrying_amount"], "10600.0000")

        # A reversal entry is not itself eligible for another reversal.
        reverse_again = self.client.post(
            f"/api/accounting/journal-entries/{reversed_entry.data['id']}/reverse/",
            {"entry_date": "2026-04-03"}, format="json",
        )
        self.assertEqual(reverse_again.status_code, 400, reverse_again.data)

    def test_reversed_customer_fx_revaluation_allows_receipt_post_and_reversal(self):
        delivery_id, line_id = self._create_delivery("DEL-FX-REV-RECEIPT")
        usd = Currency.objects.create(code="USD", name_ar="دولار أمريكي")
        for rate_date, rate in (
            (date(2026, 3, 10), "50"),
            (date(2026, 3, 31), "53"),
        ):
            ExchangeRate.objects.create(
                company=self.company, currency=usd,
                rate_date=rate_date, rate=Decimal(rate),
            )
        invoice = self.client.post("/api/sales-documents/invoices/", {
            "company": self.company.pk, "customer": self.customer.pk,
            "delivery": delivery_id, "invoice_number": "INV-FX-REV-RECEIPT",
            "invoice_date": "2026-03-10",
            "transaction_currency": usd.pk, "exchange_rate": "50",
            "exchange_rate_date": "2026-03-10",
            "lines": [{"delivery_line": line_id, "transaction_unit_price": "100", "tax_rate": "0"}],
        }, format="json")
        self.assertEqual(invoice.status_code, 201, invoice.data)
        invoice_id = invoice.data["id"]
        posted_invoice = self.client.post(
            f"/api/sales-documents/invoices/{invoice_id}/post/", {}, format="json"
        )
        self.assertEqual(posted_invoice.status_code, 200, posted_invoice.data)

        revalued = self.client.post(
            f"/api/sales-documents/invoices/{invoice_id}/revalue/",
            {"revaluation_date": "2026-03-31", "rate_date": "2026-03-31", "rate": "53"},
            format="json",
        )
        self.assertEqual(revalued.status_code, 201, revalued.data)
        reversed_entry = self.client.post(
            f"/api/accounting/journal-entries/{revalued.data['journal_entry']}/reverse/",
            {"entry_date": "2026-04-01"}, format="json",
        )
        self.assertEqual(reversed_entry.status_code, 201, reversed_entry.data)

        receipt = self.client.post("/api/sales-documents/receipts/", {
            "company": self.company.pk, "customer": self.customer.pk,
            "invoice": invoice_id, "receipt_number": "RC-FX-REV-RECEIPT",
            "receipt_date": "2026-03-15", "method": "cash",
            "amount": "1000", "transaction_currency": usd.pk,
            "transaction_amount": "20", "exchange_rate": "50",
            "exchange_rate_date": "2026-03-10",
        }, format="json")
        self.assertEqual(receipt.status_code, 201, receipt.data)
        posted_receipt = self.client.post(
            f"/api/sales-documents/receipts/{receipt.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(posted_receipt.status_code, 200, posted_receipt.data)

        reversed_receipt = self.client.post(
            f"/api/sales-documents/receipts/{receipt.data['id']}/reverse/",
            {"entry_date": "2026-04-02"}, format="json",
        )
        self.assertEqual(reversed_receipt.status_code, 200, reversed_receipt.data)
        self.assertEqual(reversed_receipt.data["status"], "reversed")

    def test_fx_sales_return_allows_post_and_reverse_after_revaluation_is_reversed(self):
        delivery_id, line_id = self._create_delivery("DEL-FX-RETURN-REVERSED")
        usd = Currency.objects.create(code="USD", name_ar="دولار أمريكي")
        for rate_date, rate in (
            (date(2026, 3, 10), "50"),
            (date(2026, 3, 31), "53"),
        ):
            ExchangeRate.objects.create(
                company=self.company, currency=usd,
                rate_date=rate_date, rate=Decimal(rate),
            )
        invoice = self.client.post("/api/sales-documents/invoices/", {
            "company": self.company.pk, "customer": self.customer.pk,
            "delivery": delivery_id, "invoice_number": "INV-FX-RETURN-REVERSED",
            "invoice_date": "2026-03-10",
            "transaction_currency": usd.pk, "exchange_rate": "50",
            "exchange_rate_date": "2026-03-10",
            "lines": [{"delivery_line": line_id, "transaction_unit_price": "100", "tax_rate": "0"}],
        }, format="json")
        self.assertEqual(invoice.status_code, 201, invoice.data)
        invoice_id = invoice.data["id"]
        posted_invoice = self.client.post(
            f"/api/sales-documents/invoices/{invoice_id}/post/", {}, format="json"
        )
        self.assertEqual(posted_invoice.status_code, 200, posted_invoice.data)

        revalued = self.client.post(
            f"/api/sales-documents/invoices/{invoice_id}/revalue/",
            {"revaluation_date": "2026-03-31", "rate_date": "2026-03-31", "rate": "53"},
            format="json",
        )
        self.assertEqual(revalued.status_code, 201, revalued.data)

        created = self.client.post("/api/sales-documents/returns/", {
            "company": self.company.pk, "invoice": invoice_id,
            "return_number": "RET-FX-REVALUED", "return_date": "2026-04-02",
            "lines": [{"invoice_line": invoice.data["invoice_lines"][0]["id"], "quantity": "1"}],
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        blocked_while_active = self.client.post(
            f"/api/sales-documents/returns/{created.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(blocked_while_active.status_code, 400, blocked_while_active.data)

        reversed_revaluation = self.client.post(
            f"/api/accounting/journal-entries/{revalued.data['journal_entry']}/reverse/",
            {"entry_date": "2026-04-01"}, format="json",
        )
        self.assertEqual(reversed_revaluation.status_code, 201, reversed_revaluation.data)

        posted_return = self.client.post(
            f"/api/sales-documents/returns/{created.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(posted_return.status_code, 200, posted_return.data)
        self.assertEqual(posted_return.data["status"], "posted")
        self.assertEqual(posted_return.data["transaction_currency_code"], "USD")
        self.assertEqual(posted_return.data["transaction_grand_total"], "100.0000")

        reversed_return = self.client.post(
            f"/api/sales-documents/returns/{created.data['id']}/reverse/",
            {"entry_date": "2026-04-03"}, format="json",
        )
        self.assertEqual(reversed_return.status_code, 200, reversed_return.data)
        self.assertEqual(reversed_return.data["status"], "reversed")

    def test_customer_fx_revaluation_is_released_to_realized_fx_on_partial_and_final_receipts(self):
        delivery_id, line_id = self._create_delivery("DEL-FX-REVAL")
        usd = Currency.objects.create(code="USD", name_ar="دولار أمريكي")
        for rate_date, rate in (
            (date(2026, 3, 10), "50"), (date(2026, 3, 31), "53"),
            (date(2026, 4, 1), "52"), (date(2026, 4, 2), "50"),
        ):
            ExchangeRate.objects.create(company=self.company, currency=usd, rate_date=rate_date, rate=Decimal(rate))
        created = self.client.post("/api/sales-documents/invoices/", {
            "company": self.company.pk, "customer": self.customer.pk, "delivery": delivery_id,
            "invoice_number": "INV-FX-REVAL", "invoice_date": "2026-03-10",
            "transaction_currency": usd.pk, "exchange_rate": "50", "exchange_rate_date": "2026-03-10",
            "lines": [{"delivery_line": line_id, "transaction_unit_price": "100", "tax_rate": "0"}],
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        invoice_id = created.data["id"]
        posted = self.client.post(f"/api/sales-documents/invoices/{invoice_id}/post/", {}, format="json")
        self.assertEqual(posted.status_code, 200, posted.data)

        revalued = self.client.post(f"/api/sales-documents/invoices/{invoice_id}/revalue/", {
            "revaluation_date": "2026-03-31", "rate_date": "2026-03-31", "rate": "53",
        }, format="json")
        self.assertEqual(revalued.status_code, 201, revalued.data)
        self.assertEqual(revalued.data["previous_carrying_amount"], "10000.0000")
        self.assertEqual(revalued.data["new_carrying_amount"], "10600.0000")
        self.assertEqual(revalued.data["difference"], "600.0000")
        self.assertTrue(JournalLine.objects.filter(
            entry_id=revalued.data["journal_entry"], account__code="7210",
            credit=Decimal("600.0000"),
        ).exists())

        first = self.client.post("/api/sales-documents/receipts/", {
            "company": self.company.pk, "customer": self.customer.pk, "invoice": invoice_id,
            "receipt_number": "RC-FX-REVAL-1", "receipt_date": "2026-04-01", "method": "cash",
            "amount": "2080.0000", "transaction_currency": usd.pk, "transaction_amount": "40",
            "exchange_rate": "52", "exchange_rate_date": "2026-04-01",
        }, format="json")
        self.assertEqual(first.status_code, 201, first.data)
        first_posted = self.client.post(f"/api/sales-documents/receipts/{first.data['id']}/post/", {}, format="json")
        self.assertEqual(first_posted.status_code, 200, first_posted.data)
        self.assertEqual(first_posted.data["carrying_base_amount"], "2120.0000")
        self.assertEqual(first_posted.data["historical_carrying_base_amount"], "2000.0000")
        self.assertEqual(first_posted.data["realized_fx_gain"], "80.0000")
        self.assertEqual(first_posted.data["realized_fx_loss"], "0.0000")

        final = self.client.post("/api/sales-documents/receipts/", {
            "company": self.company.pk, "customer": self.customer.pk, "invoice": invoice_id,
            "receipt_number": "RC-FX-REVAL-2", "receipt_date": "2026-04-02", "method": "cash",
            "amount": "8000.0000", "transaction_currency": usd.pk, "transaction_amount": "160",
            "exchange_rate": "50", "exchange_rate_date": "2026-04-02",
        }, format="json")
        self.assertEqual(final.status_code, 201, final.data)
        final_posted = self.client.post(f"/api/sales-documents/receipts/{final.data['id']}/post/", {}, format="json")
        self.assertEqual(final_posted.status_code, 200, final_posted.data)
        self.assertEqual(final_posted.data["carrying_base_amount"], "8480.0000")
        self.assertEqual(final_posted.data["historical_carrying_base_amount"], "8000.0000")
        self.assertEqual(final_posted.data["realized_fx_gain"], "0.0000")
        self.assertEqual(final_posted.data["realized_fx_loss"], "0.0000")
        self.assertTrue(JournalLine.objects.filter(
            entry_id=final_posted.data["journal_entry"], account__code="7210",
            debit=Decimal("480.0000"),
        ).exists())
        blocked_reversal = self.client.post(
            f"/api/accounting/journal-entries/{revalued.data['journal_entry']}/reverse/",
            {"entry_date": "2026-04-03"}, format="json",
        )
        self.assertEqual(blocked_reversal.status_code, 400, blocked_reversal.data)

    def test_latest_customer_receipt_can_be_reversed_with_linked_journal(self):
        delivery_id, line_id = self._create_delivery("DEL-REV-001")
        invoice = self.client.post("/api/sales-documents/invoices/", {
            "company": self.company.pk, "customer": self.customer.pk, "delivery": delivery_id,
            "invoice_number": "INV-REV-001", "invoice_date": "2026-03-10",
            "lines": [{"delivery_line": line_id, "unit_price": "100", "tax_rate": "14"}],
        }, format="json")
        self.assertEqual(invoice.status_code, 201, invoice.data)
        invoice_id = invoice.data["id"]
        posted_invoice = self.client.post(f"/api/sales-documents/invoices/{invoice_id}/post/", {}, format="json")
        self.assertEqual(posted_invoice.status_code, 200, posted_invoice.data)
        from treasury.models import TreasuryAccount
        bank = ChartOfAccount.objects.get(company=self.company, code="1100")
        TreasuryAccount.objects.create(company=self.company, branch=self.branch, account=bank,
            kind=TreasuryAccount.Kind.BANK, code="BANK-REV", name_ar="بنك العكس", currency_code="EGP")
        receipt = self.client.post("/api/sales-documents/receipts/", {
            "company": self.company.pk, "customer": self.customer.pk, "invoice": invoice_id,
            "receipt_number": "RC-REV-001", "receipt_date": "2026-03-11",
            "method": "bank", "payment_account": bank.pk, "amount": "100",
        }, format="json")
        self.assertEqual(receipt.status_code, 201, receipt.data)
        receipt_id = receipt.data["id"]
        posted_receipt = self.client.post(f"/api/sales-documents/receipts/{receipt_id}/post/", {}, format="json")
        self.assertEqual(posted_receipt.status_code, 200, posted_receipt.data)
        original_entry_id = posted_receipt.data["journal_entry"]

        reversed_response = self.client.post(
            f"/api/sales-documents/receipts/{receipt_id}/reverse/",
            {"entry_date": "2026-03-12"}, format="json",
        )
        self.assertEqual(reversed_response.status_code, 200, reversed_response.data)
        self.assertEqual(reversed_response.data["status"], "reversed")
        reversal_id = reversed_response.data["reversal_journal_entry"]
        original = JournalEntry.objects.get(pk=original_entry_id)
        reversal = JournalEntry.objects.get(pk=reversal_id)
        self.assertEqual(original.status, JournalEntry.Status.REVERSED)
        self.assertEqual(reversal.reversal_of_id, original.pk)
        self.assertEqual(reversal.status, JournalEntry.Status.POSTED)
        invoice_after = self.client.get(f"/api/sales-documents/invoices/{invoice_id}/").data
        self.assertEqual(invoice_after["paid_total"], "0")
        self.assertEqual(invoice_after["balance_due"], "228.0000")

        # Historical reports must retain the receipt before its reversal date.
        before_reversal = self.client.get(
            f"/api/sales-documents/invoices/customer-aging/?company={self.company.pk}&as_of=2026-03-11"
        )
        self.assertEqual(before_reversal.status_code, 200, before_reversal.data)
        self.assertEqual(before_reversal.data["totals"]["total_due"], "128.0000")
        after_reversal = self.client.get(
            f"/api/sales-documents/invoices/customer-aging/?company={self.company.pk}&as_of=2026-03-12"
        )
        self.assertEqual(after_reversal.status_code, 200, after_reversal.data)
        self.assertEqual(after_reversal.data["totals"]["total_due"], "228.0000")

        statement_before_reversal = self.client.get(
            f"/api/sales-documents/invoices/customer-statement/?company={self.company.pk}"
            f"&customer={self.customer.pk}&date_from=2026-03-01&date_to=2026-03-11"
        )
        self.assertEqual(statement_before_reversal.status_code, 200, statement_before_reversal.data)
        self.assertEqual(len(statement_before_reversal.data["rows"]), 2)
        self.assertEqual(statement_before_reversal.data["closing_balance"], "128.0000")
        statement_after_reversal = self.client.get(
            f"/api/sales-documents/invoices/customer-statement/?company={self.company.pk}"
            f"&customer={self.customer.pk}&date_from=2026-03-01&date_to=2026-03-12"
        )
        self.assertEqual(statement_after_reversal.status_code, 200, statement_after_reversal.data)
        self.assertEqual(len(statement_after_reversal.data["rows"]), 3)
        self.assertEqual(statement_after_reversal.data["rows"][2]["document_type"], "receipt_reversal")
        self.assertEqual(statement_after_reversal.data["closing_balance"], "228.0000")

        repeated = self.client.post(
            f"/api/sales-documents/receipts/{receipt_id}/reverse/",
            {"entry_date": "2026-03-13"}, format="json",
        )
        self.assertEqual(repeated.status_code, 400, repeated.data)

    def test_customer_default_discount_and_lower_approval_limit_apply_to_new_invoice(self):
        self.customer.default_discount_percent = Decimal("5")
        self.customer.discount_approval_limit = Decimal("3")
        self.customer.save(update_fields=("default_discount_percent", "discount_approval_limit"))

        delivery_id, line_id = self._create_delivery("DEL-CUSTOMER-DISCOUNT")
        response = self.client.post("/api/sales-documents/invoices/", {
            "company": self.company.pk,
            "customer": self.customer.pk,
            "delivery": delivery_id,
            "invoice_number": "INV-CUSTOMER-DISCOUNT",
            "invoice_date": "2026-03-10",
            "lines": [{"delivery_line": line_id, "unit_price": "100", "tax_rate": "0"}],
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["invoice_lines"][0]["discount_percent"], "5.0000")
        self.assertEqual(response.data["discount_total"], "10.0000")
        self.assertEqual(response.data["discount_approval_limit"], "3.0000")
        self.assertEqual(response.data["approval_status"], "pending")

        blocked = self.client.post(
            f"/api/sales-documents/invoices/{response.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(blocked.status_code, 400, blocked.data)
        self.assertEqual(JournalEntry.objects.filter(
            source_type=JournalEntry.Source.SALES, status=JournalEntry.Status.POSTED
        ).count(), 0)

    def test_discount_rules_apply_one_highest_priority_rule_and_keep_manual_override(self):
        specific_rule = CustomerDiscountRule.objects.create(
            company=self.company, customer=self.customer, product=self.product,
            min_quantity=Decimal("2"), min_invoice_amount=Decimal("150"),
            discount_percent=Decimal("12"), priority=10,
            effective_from=date(2026, 1, 1), notes="خصم عميل وصنف",
        )
        CustomerDiscountRule.objects.create(
            company=self.company, customer_type=Customer.CustomerType.COMPANY,
            discount_percent=Decimal("7"), priority=5,
            effective_from=date(2026, 1, 1), notes="خصم فئة العملاء",
        )
        delivery_id, line_id = self._create_delivery("DEL-DISCOUNT-RULE")
        response = self.client.post("/api/sales-documents/invoices/", {
            "company": self.company.pk, "customer": self.customer.pk,
            "delivery": delivery_id, "invoice_number": "INV-DISCOUNT-RULE",
            "invoice_date": "2026-03-10",
            "lines": [{"delivery_line": line_id, "unit_price": "100", "tax_rate": "0"}],
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["invoice_lines"][0]["discount_percent"], "12.0000")
        self.assertEqual(response.data["invoice_lines"][0]["discount_rule"], specific_rule.pk)
        self.assertEqual(response.data["discount_total"], "24.0000")

    def test_explicit_invoice_discount_overrides_rules_without_stacking(self):
        CustomerDiscountRule.objects.create(
            company=self.company, customer=self.customer,
            discount_percent=Decimal("15"), priority=100,
            effective_from=date(2026, 1, 1), notes="قاعدة العميل",
        )
        delivery_id, line_id = self._create_delivery("DEL-DISCOUNT-OVERRIDE")
        response = self.client.post("/api/sales-documents/invoices/", {
            "company": self.company.pk, "customer": self.customer.pk,
            "delivery": delivery_id, "invoice_number": "INV-DISCOUNT-OVERRIDE",
            "invoice_date": "2026-03-10",
            "lines": [{"delivery_line": line_id, "unit_price": "100", "tax_rate": "0", "discount_percent": "2"}],
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["invoice_lines"][0]["discount_percent"], "2.0000")
        self.assertIsNone(response.data["invoice_lines"][0]["discount_rule"])
        self.assertEqual(response.data["discount_total"], "4.0000")

    def test_quotation_customer_change_requires_lines_for_repricing(self):
        another_customer = Customer.objects.create(
            company=self.company, code="CUS-2", name_ar="عميل ثان",
            default_discount_percent=Decimal("5"),
        )
        created = self.client.post("/api/sales-documents/quotations/", {
            "company": self.company.pk,
            "customer": self.customer.pk,
            "quotation_number": "Q-REPRICE",
            "quotation_date": "2027-01-10",
            "valid_until": "2027-01-20",
            "lines": [{"product": self.product.pk, "quantity": "1", "unit_price": "100"}],
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)

        response = self.client.patch(
            f"/api/sales-documents/quotations/{created.data['id']}/",
            {"customer": another_customer.pk},
            format="json",
        )
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("lines", response.data)
        quotation = SalesQuotation.objects.get(pk=created.data["id"])
        self.assertEqual(quotation.customer_id, self.customer.pk)
        self.assertEqual(quotation.discount_total, Decimal("0.0000"))

    def test_quotation_cannot_be_moved_to_another_company(self):
        other_company = Company.objects.create(
            code="OTHER-SELL", name_ar="شركة أخرى", country="EG",
            activity="commercial", base_currency=self.company.base_currency,
        )
        other_customer = Customer.objects.create(
            company=other_company, code="CUS-OTHER", name_ar="عميل آخر",
        )
        created = self.client.post("/api/sales-documents/quotations/", {
            "company": self.company.pk,
            "customer": self.customer.pk,
            "quotation_number": "Q-COMPANY-LOCK",
            "quotation_date": "2027-01-10",
            "valid_until": "2027-01-20",
            "lines": [{"product": self.product.pk, "quantity": "1", "unit_price": "100"}],
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)

        response = self.client.patch(
            f"/api/sales-documents/quotations/{created.data['id']}/",
            {"company": other_company.pk, "customer": other_customer.pk},
            format="json",
        )
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("company", response.data)
        self.assertEqual(
            SalesQuotation.objects.get(pk=created.data["id"]).company_id,
            self.company.pk,
        )

    def test_accepted_quotation_converts_once_to_draft_delivery_without_stock_or_ledger_effects(self):
        opening_movements = StockMovement.objects.count()
        opening_journals = JournalEntry.objects.count()
        created = self.client.post("/api/sales-documents/quotations/", {
            "company": self.company.pk,
            "customer": self.customer.pk,
            "quotation_number": "Q-2026-001",
            "quotation_date": "2027-01-10",
            "valid_until": "2027-01-20",
            "lines": [{"product": self.product.pk, "quantity": "2", "unit_price": "100", "tax_rate": "14"}],
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        quote_id = created.data["id"]
        self.assertEqual(created.data["status"], "draft")
        self.assertEqual(created.data["grand_total"], "228.0000")
        self.assertEqual(StockMovement.objects.count(), opening_movements)
        self.assertEqual(JournalEntry.objects.count(), opening_journals)

        sent = self.client.post(f"/api/sales-documents/quotations/{quote_id}/send/", {}, format="json")
        self.assertEqual(sent.status_code, 200, sent.data)
        accepted = self.client.post(f"/api/sales-documents/quotations/{quote_id}/accept/", {}, format="json")
        self.assertEqual(accepted.status_code, 200, accepted.data)

        original_select_for_update = QuerySet.select_for_update
        lock_calls = []

        def track_row_lock(queryset, *args, **kwargs):
            lock_calls.append(kwargs)
            return original_select_for_update(queryset, *args, **kwargs)

        with patch.object(QuerySet, "select_for_update", track_row_lock):
            converted = self.client.post(f"/api/sales-documents/quotations/{quote_id}/convert-to-delivery/", {
                "warehouse": self.warehouse.pk,
                "delivery_number": "DEL-FROM-Q-001",
                "delivery_date": "2027-01-11",
            }, format="json")
        self.assertIn({"of": ("self",)}, lock_calls)
        self.assertEqual(converted.status_code, 201, converted.data)
        self.assertEqual(converted.data["movement_status"], "draft")
        self.assertEqual(converted.data["quotation_snapshot"]["quotation_number"], "Q-2026-001")
        self.assertEqual(converted.data["quotation_snapshot"]["lines"][0]["discount_percent"], "0.0000")
        self.assertEqual(StockMovement.objects.count(), opening_movements + 1)
        self.assertEqual(JournalEntry.objects.count(), opening_journals)

        repeated = self.client.post(f"/api/sales-documents/quotations/{quote_id}/convert-to-delivery/", {
            "warehouse": self.warehouse.pk, "delivery_number": "DEL-DUPLICATE",
            "delivery_date": "2026-03-11",
        }, format="json")
        self.assertEqual(repeated.status_code, 200, repeated.data)
        self.assertEqual(repeated.data["delivery"]["id"], converted.data["id"])
        self.assertEqual(StockMovement.objects.count(), opening_movements + 1)
        quote = SalesQuotation.objects.get(pk=quote_id)
        self.assertEqual(quote.status, SalesQuotation.Status.CONVERTED)
        self.assertEqual(quote.converted_delivery_id, converted.data["id"])

    def test_expired_quotation_cannot_be_sent_or_converted(self):
        created = self.client.post("/api/sales-documents/quotations/", {
            "company": self.company.pk, "customer": self.customer.pk,
            "quotation_number": "Q-EXPIRED", "quotation_date": "2026-01-01",
            "valid_until": "2026-01-02",
            "lines": [{"product": self.product.pk, "quantity": "1", "unit_price": "100"}],
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        response = self.client.post(f"/api/sales-documents/quotations/{created.data['id']}/send/", {}, format="json")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(SalesQuotation.objects.get(pk=created.data["id"]).status, SalesQuotation.Status.EXPIRED)

    def test_expired_accepted_quotation_keeps_expired_status_when_conversion_is_rejected(self):
        created = self.client.post("/api/sales-documents/quotations/", {
            "company": self.company.pk,
            "customer": self.customer.pk,
            "quotation_number": "Q-EXPIRED-CONVERT",
            "quotation_date": "2027-01-10",
            "valid_until": "2027-01-20",
            "lines": [{"product": self.product.pk, "quantity": "1", "unit_price": "100"}],
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        quote_id = created.data["id"]

        sent = self.client.post(
            f"/api/sales-documents/quotations/{quote_id}/send/", {}, format="json"
        )
        self.assertEqual(sent.status_code, 200, sent.data)
        accepted = self.client.post(
            f"/api/sales-documents/quotations/{quote_id}/accept/", {}, format="json"
        )
        self.assertEqual(accepted.status_code, 200, accepted.data)

        movement_count = StockMovement.objects.count()
        with patch("sales.api.timezone.localdate", return_value=date(2027, 1, 21)):
            response = self.client.post(
                f"/api/sales-documents/quotations/{quote_id}/convert-to-delivery/",
                {
                    "warehouse": self.warehouse.pk,
                    "delivery_number": "DEL-EXPIRED-QUOTE",
                    "delivery_date": "2027-01-21",
                },
                format="json",
            )

        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(
            SalesQuotation.objects.get(pk=quote_id).status,
            SalesQuotation.Status.EXPIRED,
        )
        self.assertEqual(StockMovement.objects.count(), movement_count)

    def test_quotation_send_captures_company_default_print_template_snapshot(self):
        template = SalesDocumentTemplate.objects.create(
            company=self.company,
            document_type=SalesDocumentTemplate.DocumentType.QUOTATION,
            name="Quotation Modern",
            style=SalesDocumentTemplate.Style.MODERN,
            config={"accent_color": "#224466", "show_discount": True},
            is_default=True,
        )
        created = self.client.post("/api/sales-documents/quotations/", {
            "company": self.company.pk, "customer": self.customer.pk,
            "quotation_number": "Q-TEMPLATE-001", "quotation_date": "2027-01-10",
            "valid_until": "2027-01-20",
            "lines": [{"product": self.product.pk, "quantity": "1", "unit_price": "100"}],
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        sent = self.client.post(
            f"/api/sales-documents/quotations/{created.data['id']}/send/", {}, format="json"
        )
        self.assertEqual(sent.status_code, 200, sent.data)
        self.assertEqual(sent.data["print_template_snapshot"]["template_id"], template.pk)
        self.assertEqual(sent.data["print_template_snapshot"]["style"], "modern")
        template.config = {"accent_color": "#AA0000"}
        template.save(update_fields=("config", "updated_at"))
        refreshed = self.client.get(f"/api/sales-documents/quotations/{created.data['id']}/")
        self.assertEqual(refreshed.data["print_template_snapshot"]["config"]["accent_color"], "#224466")

    def test_only_company_admin_can_manage_document_templates_and_default_is_unique(self):
        admin = get_user_model().objects.create_user(username="sales_template_admin", password="test-password")
        CompanyMembership.objects.create(
            user=admin, company=self.company, role="admin", all_branches=True
        )
        self.client.force_authenticate(admin)
        first = self.client.post("/api/sales-documents/document-templates/", {
            "company": self.company.pk, "document_type": "invoice",
            "name": "Classic Invoice", "style": "classic", "is_default": True,
            "config": {"show_tax": True},
        }, format="json")
        self.assertEqual(first.status_code, 201, first.data)
        second = self.client.post("/api/sales-documents/document-templates/", {
            "company": self.company.pk, "document_type": "invoice",
            "name": "Modern Invoice", "style": "modern", "is_default": True,
            "config": {"accent_color": "#123456"},
        }, format="json")
        self.assertEqual(second.status_code, 201, second.data)
        self.assertFalse(SalesDocumentTemplate.objects.get(pk=first.data["id"]).is_default)
        self.assertTrue(SalesDocumentTemplate.objects.get(pk=second.data["id"]).is_default)
        purchase_template = self.client.post("/api/sales-documents/document-templates/", {
            "company": self.company.pk, "document_type": "purchase_invoice",
            "name": "Supplier Invoice Modern", "style": "modern", "is_default": True,
            "config": {"accent_color": "#345678", "show_tax": True},
        }, format="json")
        self.assertEqual(purchase_template.status_code, 201, purchase_template.data)
        self.assertEqual(purchase_template.data["document_type"], "purchase_invoice")
        listed = self.client.get(f"/api/sales-documents/document-templates/?company={self.company.pk}&document_type=purchase_invoice")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual([row["id"] for row in listed.data], [purchase_template.data["id"]])

        self.client.force_authenticate(self.user)
        denied = self.client.post("/api/sales-documents/document-templates/", {
            "company": self.company.pk, "document_type": "invoice",
            "name": "Accountant Template", "style": "compact",
        }, format="json")
        self.assertEqual(denied.status_code, 403)


    def test_invoice_posting_rolls_back_created_journal_when_accounting_post_fails(self):
        from django.core.exceptions import ValidationError as DjangoValidationError

        delivery_id, line_id = self._create_delivery("DEL-ATOMIC-POST")
        created = self.client.post("/api/sales-documents/invoices/", {
            "company": self.company.pk,
            "customer": self.customer.pk,
            "delivery": delivery_id,
            "invoice_number": "INV-ATOMIC-POST",
            "invoice_date": "2026-03-10",
            "lines": [{"delivery_line": line_id, "unit_price": "100", "tax_rate": "14"}],
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        invoice_id = created.data["id"]
        journal_count_before = JournalEntry.objects.count()

        with patch(
            "sales.services.post_journal",
            side_effect=DjangoValidationError("محاكاة فشل التحقق المحاسبي"),
        ):
            response = self.client.post(
                f"/api/sales-documents/invoices/{invoice_id}/post/", {}, format="json"
            )

        self.assertEqual(response.status_code, 400, response.data)
        invoice = self.client.get(f"/api/sales-documents/invoices/{invoice_id}/").data
        self.assertEqual(invoice["status"], "draft")
        self.assertIsNone(invoice["journal_entry"])
        self.assertEqual(JournalEntry.objects.count(), journal_count_before)
        self.assertFalse(JournalEntry.objects.filter(
            entry_number=f"SALE-INV-{invoice_id}"
        ).exists())


    def test_service_invoice_posts_revenue_and_receipt_without_stock_movement(self):
        service = Product.objects.create(
            company=self.company, unit=self.product.unit, code="SRV-001",
            name_ar="خدمة صيانة", product_type=Product.ProductType.SERVICE,
            stock_class=None, sale_price=Decimal("100"),
        )
        movements_before = StockMovement.objects.count()
        created = self.client.post("/api/sales-documents/invoices/", {
            "company": self.company.pk,
            "customer": self.customer.pk,
            "branch": self.branch.pk,
            "invoice_number": "INV-SERVICE-001",
            "invoice_date": "2026-03-12",
            "lines": [{
                "product": service.pk, "quantity": "2",
                "unit_price": "100", "tax_rate": "14",
            }],
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        invoice_id = created.data["id"]
        self.assertIsNone(created.data["delivery"])
        self.assertEqual(created.data["invoice_lines"][0]["product"], service.pk)
        self.assertEqual(created.data["invoice_lines"][0]["quantity"], "2.0000")

        posted = self.client.post(
            f"/api/sales-documents/invoices/{invoice_id}/post/", {}, format="json"
        )
        self.assertEqual(posted.status_code, 200, posted.data)
        self.assertEqual(posted.data["status"], "posted")
        self.assertEqual(posted.data["grand_total"], "228.0000")
        self.assertEqual(StockMovement.objects.count(), movements_before)

        revenue = ChartOfAccount.objects.get(company=self.company, code="4000")
        receivable = ChartOfAccount.objects.get(company=self.company, code="1200")
        self.assertTrue(JournalLine.objects.filter(
            entry_id=posted.data["journal_entry"], account=revenue,
            credit=Decimal("200.0000"),
        ).exists())
        self.assertTrue(JournalLine.objects.filter(
            entry_id=posted.data["journal_entry"], account=receivable,
            debit=Decimal("228.0000"),
        ).exists())

        receipt = self.client.post("/api/sales-documents/receipts/", {
            "company": self.company.pk, "customer": self.customer.pk,
            "invoice": invoice_id, "receipt_number": "RC-SERVICE-001",
            "receipt_date": "2026-03-13", "method": "bank", "amount": "228",
        }, format="json")
        self.assertEqual(receipt.status_code, 201, receipt.data)
        receipt_posted = self.client.post(
            f"/api/sales-documents/receipts/{receipt.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(receipt_posted.status_code, 200, receipt_posted.data)
        self.assertEqual(receipt_posted.data["status"], "posted")
        self.assertEqual(
            self.client.get(f"/api/sales-documents/invoices/{invoice_id}/").data["balance_due"],
            "0.0000",
        )


    def test_foreign_currency_service_invoice_exposes_print_amounts_in_transaction_currency(self):
        service = Product.objects.create(
            company=self.company, unit=self.product.unit, code="SRV-FX-PRINT",
            name_ar="خدمة بالدولار", product_type=Product.ProductType.SERVICE,
            stock_class=None, sale_price=Decimal("100"),
        )
        usd = Currency.objects.create(code="USD", name_ar="دولار أمريكي")
        ExchangeRate.objects.create(
            company=self.company, currency=usd,
            rate_date=date(2026, 3, 12), rate=Decimal("50"),
        )
        created = self.client.post("/api/sales-documents/invoices/", {
            "company": self.company.pk, "customer": self.customer.pk,
            "branch": self.branch.pk, "invoice_number": "INV-SERVICE-FX-PRINT",
            "invoice_date": "2026-03-12", "transaction_currency": usd.pk,
            "exchange_rate": "50", "exchange_rate_date": "2026-03-12",
            "lines": [{
                "product": service.pk, "quantity": "2",
                "transaction_unit_price": "100", "tax_rate": "14",
                "discount_percent": "10",
            }],
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        self.assertEqual(created.data["transaction_currency_code"], "USD")
        self.assertEqual(created.data["transaction_net_total"], "180.0000")
        self.assertEqual(created.data["transaction_discount_total"], "20.0000")
        self.assertEqual(created.data["transaction_tax_total"], "25.2000")
        self.assertEqual(created.data["transaction_grand_total"], "205.2000")
        line = created.data["invoice_lines"][0]
        self.assertEqual(line["transaction_unit_price"], "100.0000")
        self.assertEqual(line["transaction_discount_amount"], "20.0000")
        self.assertEqual(line["transaction_tax_amount"], "25.2000")
        self.assertEqual(line["transaction_total_amount"], "205.2000")

    def test_foreign_currency_service_return_preserves_transaction_amounts_without_stock(self):
        service = Product.objects.create(
            company=self.company, unit=self.product.unit, code="SRV-FX-RETURN",
            name_ar="خدمة صيانة بالدولار", product_type=Product.ProductType.SERVICE,
            stock_class=None, sale_price=Decimal("100"),
        )
        usd = Currency.objects.create(code="USD", name_ar="دولار أمريكي")
        ExchangeRate.objects.create(
            company=self.company, currency=usd,
            rate_date=date(2026, 3, 12), rate=Decimal("50"),
        )
        movements_before = StockMovement.objects.count()
        invoice = self.client.post("/api/sales-documents/invoices/", {
            "company": self.company.pk, "customer": self.customer.pk,
            "branch": self.branch.pk, "invoice_number": "INV-SERVICE-FX-RETURN",
            "invoice_date": "2026-03-12", "transaction_currency": usd.pk,
            "exchange_rate": "50", "exchange_rate_date": "2026-03-12",
            "lines": [{
                "product": service.pk, "quantity": "2",
                "transaction_unit_price": "100", "tax_rate": "14",
                "discount_percent": "10",
            }],
        }, format="json")
        self.assertEqual(invoice.status_code, 201, invoice.data)
        posted_invoice = self.client.post(
            f"/api/sales-documents/invoices/{invoice.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(posted_invoice.status_code, 200, posted_invoice.data)

        created = self.client.post("/api/sales-documents/returns/", {
            "company": self.company.pk, "invoice": invoice.data["id"],
            "return_number": "RET-SERVICE-FX-RETURN",
            "return_date": "2026-03-13",
            "lines": [{
                "invoice_line": invoice.data["invoice_lines"][0]["id"],
                "quantity": "1",
            }],
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        self.assertIsNone(created.data["movement_status"])
        posted = self.client.post(
            f"/api/sales-documents/returns/{created.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(posted.status_code, 200, posted.data)
        self.assertEqual(posted.data["grand_total"], "5130.0000")
        self.assertEqual(posted.data["transaction_net_total"], "90.0000")
        self.assertEqual(posted.data["transaction_tax_total"], "12.6000")
        self.assertEqual(posted.data["transaction_grand_total"], "102.6000")
        self.assertIsNone(posted.data["cost_journal_entry"])
        self.assertIsNone(posted.data["movement_status"])
        self.assertEqual(StockMovement.objects.count(), movements_before)

        reversed_return = self.client.post(
            f"/api/sales-documents/returns/{created.data['id']}/reverse/",
            {"entry_date": "2026-03-14"}, format="json",
        )
        self.assertEqual(reversed_return.status_code, 200, reversed_return.data)
        self.assertEqual(reversed_return.data["status"], "reversed")
        self.assertIsNone(reversed_return.data["reversal_stock_movement"])
        self.assertEqual(StockMovement.objects.count(), movements_before)


    def test_service_return_posts_credit_note_and_reverses_without_stock(self):
        service = Product.objects.create(
            company=self.company, unit=self.product.unit, code="SRV-RET-001",
            name_ar="خدمة استشارية", product_type=Product.ProductType.SERVICE,
            stock_class=None, sale_price=Decimal("100"),
        )
        movements_before = StockMovement.objects.count()
        invoice = self.client.post("/api/sales-documents/invoices/", {
            "company": self.company.pk, "customer": self.customer.pk,
            "branch": self.branch.pk, "invoice_number": "INV-SERVICE-RET-001",
            "invoice_date": "2026-03-12",
            "lines": [{"product": service.pk, "quantity": "2", "unit_price": "100", "tax_rate": "14"}],
        }, format="json")
        self.assertEqual(invoice.status_code, 201, invoice.data)
        posted_invoice = self.client.post(
            f"/api/sales-documents/invoices/{invoice.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(posted_invoice.status_code, 200, posted_invoice.data)

        created = self.client.post("/api/sales-documents/returns/", {
            "company": self.company.pk, "invoice": invoice.data["id"],
            "return_number": "RET-SERVICE-001", "return_date": "2026-03-13",
            "lines": [{"invoice_line": invoice.data["invoice_lines"][0]["id"], "quantity": "1"}],
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        self.assertIsNone(created.data["movement_status"])
        self.assertEqual(created.data["line_items"][0]["cost_amount"], "0.0000")

        posted = self.client.post(
            f"/api/sales-documents/returns/{created.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(posted.status_code, 200, posted.data)
        self.assertEqual(posted.data["grand_total"], "114.0000")
        self.assertIsNone(posted.data["cost_journal_entry"])
        self.assertEqual(StockMovement.objects.count(), movements_before)

        reversed_return = self.client.post(
            f"/api/sales-documents/returns/{created.data['id']}/reverse/",
            {"entry_date": "2026-03-14"}, format="json",
        )
        self.assertEqual(reversed_return.status_code, 200, reversed_return.data)
        self.assertEqual(reversed_return.data["status"], "reversed")
        self.assertIsNone(reversed_return.data["reversal_stock_movement"])
        self.assertEqual(StockMovement.objects.count(), movements_before)


    def test_foreign_currency_service_invoice_and_partial_receipts(self):
        service = Product.objects.create(
            company=self.company, unit=self.product.unit, code="SRV-FX-001",
            name_ar="خدمة استشارية دولية", product_type=Product.ProductType.SERVICE,
            stock_class=None, sale_price=Decimal("100"),
        )
        usd = Currency.objects.create(code="USD", name_ar="دولار أمريكي")
        ExchangeRate.objects.create(
            company=self.company, currency=usd,
            rate_date=date(2026, 3, 12), rate=Decimal("50"),
        )
        ExchangeRate.objects.create(
            company=self.company, currency=usd,
            rate_date=date(2026, 3, 13), rate=Decimal("52"),
        )
        movements_before = StockMovement.objects.count()
        created = self.client.post("/api/sales-documents/invoices/", {
            "company": self.company.pk, "customer": self.customer.pk,
            "branch": self.branch.pk, "invoice_number": "INV-SERVICE-FX-001",
            "invoice_date": "2026-03-12",
            "transaction_currency": usd.pk, "exchange_rate": "50",
            "exchange_rate_date": "2026-03-12",
            "lines": [{
                "product": service.pk, "quantity": "2",
                "transaction_unit_price": "100", "tax_rate": "14",
            }],
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        posted = self.client.post(
            f"/api/sales-documents/invoices/{created.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(posted.status_code, 200, posted.data)
        self.assertEqual(posted.data["transaction_grand_total"], "228.0000")
        self.assertEqual(posted.data["grand_total"], "11400.0000")
        self.assertEqual(StockMovement.objects.count(), movements_before)

        first = self.client.post("/api/sales-documents/receipts/", {
            "company": self.company.pk, "customer": self.customer.pk,
            "invoice": created.data["id"], "receipt_number": "RC-SERVICE-FX-001",
            "receipt_date": "2026-03-13", "method": "cash",
            "amount": "5200", "transaction_currency": usd.pk,
            "transaction_amount": "100", "exchange_rate": "52",
            "exchange_rate_date": "2026-03-13",
        }, format="json")
        self.assertEqual(first.status_code, 201, first.data)
        first_posted = self.client.post(
            f"/api/sales-documents/receipts/{first.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(first_posted.status_code, 200, first_posted.data)
        self.assertEqual(
            self.client.get(f"/api/sales-documents/invoices/{created.data['id']}/").data["balance_due"],
            "6400.0000",
        )

    def test_service_returns_cannot_exceed_original_quantity_across_documents(self):
        service = Product.objects.create(
            company=self.company, unit=self.product.unit, code="SRV-RET-LIMIT",
            name_ar="خدمة دعم فني", product_type=Product.ProductType.SERVICE,
            stock_class=None, sale_price=Decimal("100"),
        )
        movements_before = StockMovement.objects.count()
        invoice = self.client.post("/api/sales-documents/invoices/", {
            "company": self.company.pk, "customer": self.customer.pk,
            "branch": self.branch.pk, "invoice_number": "INV-SERVICE-LIMIT",
            "invoice_date": "2026-03-12",
            "lines": [{
                "product": service.pk, "quantity": "2",
                "unit_price": "100", "tax_rate": "14",
            }],
        }, format="json")
        self.assertEqual(invoice.status_code, 201, invoice.data)
        posted_invoice = self.client.post(
            f"/api/sales-documents/invoices/{invoice.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(posted_invoice.status_code, 200, posted_invoice.data)
        invoice_line_id = invoice.data["invoice_lines"][0]["id"]

        first = self.client.post("/api/sales-documents/returns/", {
            "company": self.company.pk, "invoice": invoice.data["id"],
            "return_number": "RET-SERVICE-LIMIT-1", "return_date": "2026-03-13",
            "lines": [{"invoice_line": invoice_line_id, "quantity": "1"}],
        }, format="json")
        self.assertEqual(first.status_code, 201, first.data)
        first_posted = self.client.post(
            f"/api/sales-documents/returns/{first.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(first_posted.status_code, 200, first_posted.data)

        excessive = self.client.post("/api/sales-documents/returns/", {
            "company": self.company.pk, "invoice": invoice.data["id"],
            "return_number": "RET-SERVICE-LIMIT-2", "return_date": "2026-03-14",
            "lines": [{"invoice_line": invoice_line_id, "quantity": "2"}],
        }, format="json")
        self.assertEqual(excessive.status_code, 201, excessive.data)
        rejected = self.client.post(
            f"/api/sales-documents/returns/{excessive.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(rejected.status_code, 400, rejected.data)
        self.assertEqual(StockMovement.objects.count(), movements_before)
        self.assertFalse(JournalEntry.objects.filter(
            entry_number=f"SALE-RET-{excessive.data['id']}"
        ).exists())

    def test_sales_order_draft_calculates_totals_and_confirmation_locks_changes(self):
        movements_before = StockMovement.objects.count()
        created = self.client.post("/api/sales-documents/orders/", {
            "company": self.company.pk,
            "customer": self.customer.pk,
            "warehouse": self.warehouse.pk,
            "order_number": "SO-001",
            "order_date": "2026-03-15",
            "requested_delivery_date": "2026-03-20",
            "lines": [{
                "product": self.product.pk, "quantity": "2",
                "unit_price": "100", "tax_rate": "14", "discount_percent": "10",
            }],
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        self.assertEqual(created.data["status"], "draft")
        self.assertEqual(created.data["net_total"], "180.0000")
        self.assertEqual(created.data["discount_total"], "20.0000")
        self.assertEqual(created.data["tax_total"], "25.2000")
        self.assertEqual(created.data["grand_total"], "205.2000")
        self.assertEqual(len(created.data["order_lines"]), 1)
        self.assertEqual(StockMovement.objects.count(), movements_before)

        confirmed = self.client.post(
            f"/api/sales-documents/orders/{created.data['id']}/confirm/", {}, format="json",
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.data)
        self.assertEqual(confirmed.data["status"], "confirmed")

        changed = self.client.patch(
            f"/api/sales-documents/orders/{created.data['id']}/",
            {"notes": "تعديل بعد الاعتماد"}, format="json",
        )
        self.assertEqual(changed.status_code, 400, changed.data)
        self.assertEqual(StockMovement.objects.count(), movements_before)

    def test_sales_order_rejects_services_and_invalid_delivery_date(self):
        service = Product.objects.create(
            company=self.company, unit=self.product.unit, code="SO-SERVICE",
            name_ar="خدمة غير مخزنية", product_type=Product.ProductType.SERVICE,
            stock_class=None,
        )
        invalid_service = self.client.post("/api/sales-documents/orders/", {
            "company": self.company.pk, "customer": self.customer.pk,
            "warehouse": self.warehouse.pk, "order_number": "SO-SERVICE",
            "order_date": "2026-03-15",
            "lines": [{"product": service.pk, "quantity": "1", "unit_price": "100"}],
        }, format="json")
        self.assertEqual(invalid_service.status_code, 400, invalid_service.data)

        invalid_date = self.client.post("/api/sales-documents/orders/", {
            "company": self.company.pk, "customer": self.customer.pk,
            "warehouse": self.warehouse.pk, "order_number": "SO-DATE",
            "order_date": "2026-03-15", "requested_delivery_date": "2026-03-14",
            "lines": [{"product": self.product.pk, "quantity": "1", "unit_price": "100"}],
        }, format="json")
        self.assertEqual(invalid_date.status_code, 400, invalid_date.data)


    def test_sales_order_supports_multiple_partial_deliveries_without_overdelivery(self):
        order = self.client.post("/api/sales-documents/orders/", {
            "company": self.company.pk, "customer": self.customer.pk,
            "warehouse": self.warehouse.pk, "order_number": "SO-PARTIAL-001",
            "order_date": "2026-03-10",
            "lines": [{"product": self.product.pk, "quantity": "5", "unit_price": "100", "tax_rate": "0"}],
        }, format="json")
        self.assertEqual(order.status_code, 201, order.data)
        order_line_id = order.data["order_lines"][0]["id"]
        confirmed = self.client.post(
            f"/api/sales-documents/orders/{order.data['id']}/confirm/", {}, format="json",
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.data)

        first = self.client.post("/api/sales-documents/deliveries/", {
            "company": self.company.pk, "customer": self.customer.pk,
            "warehouse": self.warehouse.pk, "order": order.data["id"],
            "delivery_number": "SO-PARTIAL-DEL-1", "delivery_date": "2026-03-11",
            "lines": [{"product": self.product.pk, "quantity": "2", "order_line": order_line_id}],
        }, format="json")
        self.assertEqual(first.status_code, 201, first.data)
        self.assertEqual(first.data["order_snapshot"]["lines"][0]["order_line_id"], order_line_id)
        self.assertEqual(first.data["order_snapshot"]["lines"][0]["unit_price"], "100.0000")

        over_delivery = self.client.post("/api/sales-documents/deliveries/", {
            "company": self.company.pk, "customer": self.customer.pk,
            "warehouse": self.warehouse.pk, "order": order.data["id"],
            "delivery_number": "SO-PARTIAL-OVER", "delivery_date": "2026-03-12",
            "lines": [{"product": self.product.pk, "quantity": "4", "order_line": order_line_id}],
        }, format="json")
        self.assertEqual(over_delivery.status_code, 400, over_delivery.data)

        first_posted = self.client.post(
            f"/api/sales-documents/deliveries/{first.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(first_posted.status_code, 200, first_posted.data)
        current = self.client.get(f"/api/sales-documents/orders/{order.data['id']}/")
        self.assertEqual(current.data["status"], "partially_delivered")

        second = self.client.post("/api/sales-documents/deliveries/", {
            "company": self.company.pk, "customer": self.customer.pk,
            "warehouse": self.warehouse.pk, "order": order.data["id"],
            "delivery_number": "SO-PARTIAL-DEL-2", "delivery_date": "2026-03-12",
            "lines": [{"product": self.product.pk, "quantity": "3", "order_line": order_line_id}],
        }, format="json")
        self.assertEqual(second.status_code, 201, second.data)
        second_posted = self.client.post(
            f"/api/sales-documents/deliveries/{second.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(second_posted.status_code, 200, second_posted.data)
        current = self.client.get(f"/api/sales-documents/orders/{order.data['id']}/")
        self.assertEqual(current.data["status"], "delivered")

        after_complete = self.client.post("/api/sales-documents/deliveries/", {
            "company": self.company.pk, "customer": self.customer.pk,
            "warehouse": self.warehouse.pk, "order": order.data["id"],
            "delivery_number": "SO-PARTIAL-AFTER", "delivery_date": "2026-03-13",
            "lines": [{"product": self.product.pk, "quantity": "1", "order_line": order_line_id}],
        }, format="json")
        self.assertEqual(after_complete.status_code, 400, after_complete.data)

    def test_sales_order_cannot_be_cancelled_while_draft_delivery_reserves_stock(self):
        order = self.client.post("/api/sales-documents/orders/", {
            "company": self.company.pk, "customer": self.customer.pk,
            "warehouse": self.warehouse.pk, "order_number": "SO-CANCEL-DRAFT",
            "order_date": "2026-03-10",
            "lines": [{"product": self.product.pk, "quantity": "3", "unit_price": "100"}],
        }, format="json")
        self.assertEqual(order.status_code, 201, order.data)
        confirmed = self.client.post(
            f"/api/sales-documents/orders/{order.data['id']}/confirm/", {}, format="json",
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.data)
        delivery = self.client.post("/api/sales-documents/deliveries/", {
            "company": self.company.pk, "customer": self.customer.pk,
            "warehouse": self.warehouse.pk, "order": order.data["id"],
            "delivery_number": "SO-CANCEL-DRAFT-DEL", "delivery_date": "2026-03-11",
            "lines": [{
                "product": self.product.pk, "quantity": "2",
                "order_line": order.data["order_lines"][0]["id"],
            }],
        }, format="json")
        self.assertEqual(delivery.status_code, 201, delivery.data)

        cancelled = self.client.post(
            f"/api/sales-documents/orders/{order.data['id']}/cancel/", {}, format="json",
        )
        self.assertEqual(cancelled.status_code, 400, cancelled.data)
        current = self.client.get(f"/api/sales-documents/orders/{order.data['id']}/")
        self.assertEqual(current.data["status"], "confirmed")
        self.assertEqual(
            self.client.get(f"/api/sales-documents/deliveries/{delivery.data['id']}/").data["movement_status"],
            "draft",
        )

    def test_sales_order_can_cancel_undelivered_remainder_without_reversing_posted_delivery(self):
        order = self.client.post("/api/sales-documents/orders/", {
            "company": self.company.pk, "customer": self.customer.pk,
            "warehouse": self.warehouse.pk, "order_number": "SO-CANCEL-PARTIAL",
            "order_date": "2026-03-10",
            "lines": [{"product": self.product.pk, "quantity": "5", "unit_price": "100"}],
        }, format="json")
        self.assertEqual(order.status_code, 201, order.data)
        confirmed = self.client.post(
            f"/api/sales-documents/orders/{order.data['id']}/confirm/", {}, format="json",
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.data)
        delivery = self.client.post("/api/sales-documents/deliveries/", {
            "company": self.company.pk, "customer": self.customer.pk,
            "warehouse": self.warehouse.pk, "order": order.data["id"],
            "delivery_number": "SO-CANCEL-PARTIAL-DEL", "delivery_date": "2026-03-11",
            "lines": [{
                "product": self.product.pk, "quantity": "2",
                "order_line": order.data["order_lines"][0]["id"],
            }],
        }, format="json")
        self.assertEqual(delivery.status_code, 201, delivery.data)
        posted = self.client.post(
            f"/api/sales-documents/deliveries/{delivery.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(posted.status_code, 200, posted.data)
        cancelled = self.client.post(
            f"/api/sales-documents/orders/{order.data['id']}/cancel/", {}, format="json",
        )
        self.assertEqual(cancelled.status_code, 200, cancelled.data)
        self.assertEqual(cancelled.data["status"], "cancelled")
        self.assertEqual(
            self.client.get(f"/api/sales-documents/deliveries/{delivery.data['id']}/").data["movement_status"],
            "posted",
        )
        self.assertEqual(
            self.client.get(f"/api/sales-documents/orders/{order.data['id']}/").data["status"],
            "cancelled",
        )

    def test_sales_workflow_end_to_end_from_quotation_to_reversed_return_and_receipt(self):
        """Exercise a customer sale from quotation through stock, ledger, settlement and reversals."""
        opening_stock = StockBalance.objects.get(product=self.product, warehouse=self.warehouse)

        quotation = self.client.post("/api/sales-documents/quotations/", {
            "company": self.company.pk,
            "customer": self.customer.pk,
            "quotation_number": "E2E-Q-001",
            "quotation_date": "2026-11-25",
            "valid_until": "2026-12-31",
            "lines": [{
                "product": self.product.pk,
                "quantity": "2",
                "unit_price": "100",
                "tax_rate": "14",
            }],
        }, format="json")
        self.assertEqual(quotation.status_code, 201, quotation.data)
        quotation_id = quotation.data["id"]
        self.assertEqual(quotation.data["grand_total"], "228.0000")

        sent = self.client.post(
            f"/api/sales-documents/quotations/{quotation_id}/send/", {}, format="json"
        )
        self.assertEqual(sent.status_code, 200, sent.data)
        accepted = self.client.post(
            f"/api/sales-documents/quotations/{quotation_id}/accept/", {}, format="json"
        )
        self.assertEqual(accepted.status_code, 200, accepted.data)

        delivery = self.client.post(
            f"/api/sales-documents/quotations/{quotation_id}/convert-to-delivery/",
            {
                "warehouse": self.warehouse.pk,
                "delivery_number": "E2E-DEL-001",
                "delivery_date": "2026-11-26",
            },
            format="json",
        )
        self.assertEqual(delivery.status_code, 201, delivery.data)
        delivery_id = delivery.data["id"]
        self.assertEqual(delivery.data["movement_status"], "draft")

        posted_delivery = self.client.post(
            f"/api/sales-documents/deliveries/{delivery_id}/post/", {}, format="json"
        )
        self.assertEqual(posted_delivery.status_code, 200, posted_delivery.data)
        delivery_balance = StockBalance.objects.get(product=self.product, warehouse=self.warehouse)
        self.assertEqual(delivery_balance.quantity, opening_stock.quantity - Decimal("2"))

        invoice = self.client.post("/api/sales-documents/invoices/", {
            "company": self.company.pk,
            "customer": self.customer.pk,
            "delivery": delivery_id,
            "invoice_number": "E2E-INV-001",
            "invoice_date": "2026-11-26",
            "lines": [{
                "delivery_line": delivery.data["line_items"][0]["id"],
                "unit_price": "100",
                "tax_rate": "14",
            }],
        }, format="json")
        self.assertEqual(invoice.status_code, 201, invoice.data)
        invoice_id = invoice.data["id"]
        posted_invoice = self.client.post(
            f"/api/sales-documents/invoices/{invoice_id}/post/", {}, format="json"
        )
        self.assertEqual(posted_invoice.status_code, 200, posted_invoice.data)
        self.assertEqual(posted_invoice.data["grand_total"], "228.0000")
        self.assertEqual(posted_invoice.data["balance_due"], "228.0000")

        self._post_customer_receipt(invoice_id, "E2E-RC-001", "2026-11-27", "100")
        receipt_id = CustomerReceipt.objects.get(receipt_number="E2E-RC-001").pk
        invoice_after_receipt = self.client.get(
            f"/api/sales-documents/invoices/{invoice_id}/"
        ).data
        self.assertEqual(invoice_after_receipt["paid_total"], "100.0000")
        self.assertEqual(invoice_after_receipt["balance_due"], "128.0000")

        sales_return = self.client.post("/api/sales-documents/returns/", {
            "company": self.company.pk,
            "invoice": invoice_id,
            "return_number": "E2E-RET-001",
            "return_date": "2026-11-28",
            "lines": [{
                "invoice_line": invoice.data["invoice_lines"][0]["id"],
                "quantity": "1",
            }],
        }, format="json")
        self.assertEqual(sales_return.status_code, 201, sales_return.data)
        posted_return = self.client.post(
            f"/api/sales-documents/returns/{sales_return.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(posted_return.status_code, 200, posted_return.data)
        self.assertEqual(posted_return.data["grand_total"], "114.0000")
        self.assertEqual(
            StockBalance.objects.get(product=self.product, warehouse=self.warehouse).quantity,
            Decimal("4.0000"),
        )
        invoice_after_return = self.client.get(
            f"/api/sales-documents/invoices/{invoice_id}/"
        ).data
        self.assertEqual(invoice_after_return["balance_due"], "14.0000")

        reversed_return = self.client.post(
            f"/api/sales-documents/returns/{sales_return.data['id']}/reverse/",
            {"entry_date": "2026-11-29"},
            format="json",
        )
        self.assertEqual(reversed_return.status_code, 200, reversed_return.data)
        self.assertEqual(reversed_return.data["status"], "reversed")
        self.assertEqual(
            StockBalance.objects.get(product=self.product, warehouse=self.warehouse).quantity,
            Decimal("3.0000"),
        )
        invoice_after_return_reversal = self.client.get(
            f"/api/sales-documents/invoices/{invoice_id}/"
        ).data
        self.assertEqual(invoice_after_return_reversal["balance_due"], "128.0000")

        reversed_receipt = self.client.post(
            f"/api/sales-documents/receipts/{receipt_id}/reverse/",
            {"entry_date": "2026-11-30"},
            format="json",
        )
        self.assertEqual(reversed_receipt.status_code, 200, reversed_receipt.data)
        self.assertEqual(reversed_receipt.data["status"], "reversed")
        final_invoice = self.client.get(f"/api/sales-documents/invoices/{invoice_id}/").data
        self.assertEqual(final_invoice["paid_total"], "0")
        self.assertEqual(final_invoice["balance_due"], "228.0000")

        posted_sales_entries = JournalEntry.objects.filter(
            source_type=JournalEntry.Source.SALES,
            status=JournalEntry.Status.POSTED,
        )
        self.assertGreaterEqual(posted_sales_entries.count(), 4)
        for entry in posted_sales_entries:
            debit_total = sum((line.debit for line in entry.lines.all()), Decimal("0"))
            credit_total = sum((line.credit for line in entry.lines.all()), Decimal("0"))
            self.assertEqual(debit_total, credit_total, entry.entry_number)

    def test_sales_performance_export_returns_real_scoped_xlsx_workbook(self):
        delivery_id, line_id = self._create_delivery("DEL-XLSX-EXPORT")
        invoice = self.client.post("/api/sales-documents/invoices/", {
            "company": self.company.pk, "customer": self.customer.pk,
            "delivery": delivery_id, "invoice_number": "INV-XLSX-EXPORT",
            "invoice_date": "2026-03-10",
            "lines": [{"delivery_line": line_id, "unit_price": "100", "tax_rate": "14"}],
        }, format="json")
        self.assertEqual(invoice.status_code, 201, invoice.data)
        posted = self.client.post(
            f"/api/sales-documents/invoices/{invoice.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(posted.status_code, 200, posted.data)

        response = self.client.get(
            "/api/sales-documents/invoices/sales-performance-export/"
            f"?company={self.company.pk}&date_from=2026-03-01&date_to=2026-03-31"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response["Content-Type"],
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        self.assertIn(".xlsx", response["Content-Disposition"])
        from io import BytesIO
        from openpyxl import load_workbook
        workbook = load_workbook(BytesIO(response.content), read_only=True, data_only=True)
        self.assertEqual(workbook.sheetnames, ["ملخص", "حسب العميل", "حسب الصنف"])
        self.assertEqual(workbook["ملخص"]["B10"].value, 1)
        customer_rows = list(workbook["حسب العميل"].iter_rows(values_only=True))
        self.assertEqual(customer_rows[1][0], self.customer.name_ar)
        product_rows = list(workbook["حسب الصنف"].iter_rows(values_only=True))
        self.assertEqual(product_rows[1][0], self.product.code)
        self.assertEqual(product_rows[1][1], self.product.name_ar)

    def test_posted_sales_documents_keep_print_template_snapshot_after_default_changes(self):
        delivery_template = SalesDocumentTemplate.objects.create(
            company=self.company, document_type="delivery", name="Delivery Frozen",
            style="modern", config={"accent_color": "#224466"}, is_default=True,
        )
        delivery_id, line_id = self._create_delivery("DEL-PRINT-SNAPSHOT")
        delivery = self.client.get(f"/api/sales-documents/deliveries/{delivery_id}/").data
        self.assertEqual(delivery["print_template_snapshot"]["template_id"], delivery_template.pk)
        self.assertEqual(delivery["print_template_snapshot"]["style"], "modern")

        invoice = self.client.post("/api/sales-documents/invoices/", {
            "company": self.company.pk, "customer": self.customer.pk,
            "delivery": delivery_id, "invoice_number": "INV-PRINT-SNAPSHOT",
            "invoice_date": "2026-03-10",
            "lines": [{"delivery_line": line_id, "unit_price": "100", "tax_rate": "0"}],
        }, format="json")
        self.assertEqual(invoice.status_code, 201, invoice.data)
        posted_invoice = self.client.post(
            f"/api/sales-documents/invoices/{invoice.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(posted_invoice.status_code, 200, posted_invoice.data)

        receipt_template = SalesDocumentTemplate.objects.create(
            company=self.company, document_type="receipt", name="Receipt Frozen",
            style="compact", config={"footer_text": "نسخة ثابتة"}, is_default=True,
        )
        receipt = self.client.post("/api/sales-documents/receipts/", {
            "company": self.company.pk, "customer": self.customer.pk,
            "invoice": invoice.data["id"], "receipt_number": "RC-PRINT-SNAPSHOT",
            "receipt_date": "2026-03-11", "method": "cash", "amount": "10",
        }, format="json")
        self.assertEqual(receipt.status_code, 201, receipt.data)
        posted_receipt = self.client.post(
            f"/api/sales-documents/receipts/{receipt.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(posted_receipt.status_code, 200, posted_receipt.data)
        self.assertEqual(posted_receipt.data["print_template_snapshot"]["template_id"], receipt_template.pk)

        batch = self.client.post("/api/sales-documents/receipt-batches/", {
            "company": self.company.pk, "customer": self.customer.pk, "branch": self.branch.pk,
            "receipt_number": "RC-BATCH-PRINT-SNAPSHOT", "receipt_date": "2026-03-11",
            "method": "cash", "amount": "5", "reference": "دفعة مقدمة",
        }, format="json")
        self.assertEqual(batch.status_code, 201, batch.data)
        posted_batch = self.client.post(
            f"/api/sales-documents/receipt-batches/{batch.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(posted_batch.status_code, 200, posted_batch.data)
        self.assertEqual(posted_batch.data["print_template_snapshot"]["template_id"], receipt_template.pk)

        return_template = SalesDocumentTemplate.objects.create(
            company=self.company, document_type="return", name="Return Frozen",
            style="modern", config={"footer_text": "مردود"}, is_default=True,
        )
        sales_return = self.client.post("/api/sales-documents/returns/", {
            "company": self.company.pk, "invoice": invoice.data["id"],
            "return_number": "RET-PRINT-SNAPSHOT", "return_date": "2026-03-12",
            "lines": [{"invoice_line": invoice.data["invoice_lines"][0]["id"], "quantity": "1"}],
        }, format="json")
        self.assertEqual(sales_return.status_code, 201, sales_return.data)
        posted_return = self.client.post(
            f"/api/sales-documents/returns/{sales_return.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(posted_return.status_code, 200, posted_return.data)
        self.assertEqual(posted_return.data["print_template_snapshot"]["template_id"], return_template.pk)

        for template in (delivery_template, receipt_template, return_template):
            template.config = {"footer_text": "تم تغيير القالب"}
            template.save(update_fields=("config", "updated_at"))
        self.assertEqual(
            SalesDocumentTemplate.objects.get(pk=delivery_template.pk).config["footer_text"],
            "تم تغيير القالب",
        )
        saved_receipt = CustomerReceipt.objects.get(pk=receipt.data["id"])
        self.assertEqual(saved_receipt.print_template_snapshot["config"]["footer_text"], "نسخة ثابتة")
        saved_batch = CustomerReceiptBatch.objects.get(pk=batch.data["id"])
        self.assertEqual(saved_batch.print_template_snapshot["config"]["footer_text"], "نسخة ثابتة")
        saved_return = self.client.get(
            f"/api/sales-documents/returns/{sales_return.data['id']}/"
        ).data
        self.assertEqual(saved_return["print_template_snapshot"]["config"]["footer_text"], "مردود")

    def test_sales_return_has_its_own_print_template_type(self):
        admin = get_user_model().objects.create_user(
            username="sales_return_template_admin", password="test-password"
        )
        CompanyMembership.objects.create(
            user=admin, company=self.company, role="admin", all_branches=True,
        )
        self.client.force_authenticate(admin)

        first = self.client.post("/api/sales-documents/document-templates/", {
            "company": self.company.pk,
            "document_type": "return",
            "name": "Return Classic",
            "style": "classic",
            "is_default": True,
            "config": {"show_tax": True, "show_discount": False},
        }, format="json")
        self.assertEqual(first.status_code, 201, first.data)
        self.assertEqual(first.data["document_type"], "return")

        second = self.client.post("/api/sales-documents/document-templates/", {
            "company": self.company.pk,
            "document_type": "return",
            "name": "Return Modern",
            "style": "modern",
            "is_default": True,
            "config": {"accent_color": "#224466"},
        }, format="json")
        self.assertEqual(second.status_code, 201, second.data)

        templates = self.client.get(
            f"/api/sales-documents/document-templates/?company={self.company.pk}&document_type=return"
        )
        self.assertEqual(templates.status_code, 200, templates.data)
        rows = list(templates.data)
        self.assertEqual(len(rows), 2)
        defaults = [row for row in rows if row["is_default"]]
        self.assertEqual(len(defaults), 1)
        self.assertEqual(defaults[0]["id"], second.data["id"])

        invoice_templates = self.client.get(
            f"/api/sales-documents/document-templates/?company={self.company.pk}&document_type=invoice"
        )
        self.assertEqual(invoice_templates.status_code, 200, invoice_templates.data)
        self.assertEqual(len(invoice_templates.data), 0)

    def test_sales_performance_allocates_delivery_cost_by_partially_invoiced_quantity(self):
        delivery_id, delivery_line_id = self._create_delivery("REPORT-PARTIAL-COST")
        invoice_ids = []
        for suffix in ("A", "B"):
            invoice = self.client.post("/api/sales-documents/invoices/", {
                "company": self.company.pk,
                "customer": self.customer.pk,
                "delivery": delivery_id,
                "invoice_number": f"REPORT-PARTIAL-{suffix}",
                "invoice_date": "2026-03-10",
                "lines": [{
                    "delivery_line": delivery_line_id,
                    "quantity": "1",
                    "unit_price": "100",
                    "tax_rate": "0",
                }],
            }, format="json")
            self.assertEqual(invoice.status_code, 201, invoice.data)
            posted = self.client.post(
                f"/api/sales-documents/invoices/{invoice.data['id']}/post/", {}, format="json"
            )
            self.assertEqual(posted.status_code, 200, posted.data)
            invoice_ids.append(invoice.data["id"])

        report = self.client.get(
            f"/api/sales-documents/invoices/sales-performance/?company={self.company.pk}"
            "&date_from=2026-03-10&date_to=2026-03-10"
        )
        self.assertEqual(report.status_code, 200, report.data)
        # The delivery costs 80 for two units. Each partial invoice must receive
        # only half of that cost; summing the full delivery-line cost per invoice
        # would incorrectly double COGS to 160.
        self.assertEqual(report.data["totals"]["invoice_count"], 2)
        self.assertEqual(report.data["totals"]["net_cost"], "80.0000")
        self.assertEqual(report.data["totals"]["gross_profit"], "120.0000")
        self.assertEqual(report.data["by_customer"][0]["net_cost"], "80.0000")
        self.assertEqual(report.data["by_product"][0]["net_cost"], "80.0000")

    def test_sales_performance_report_compares_sales_returns_and_gross_profit(self):
        delivery_id, delivery_line_id = self._create_delivery("REPORT-DEL-001")
        invoice = self.client.post("/api/sales-documents/invoices/", {
            "company": self.company.pk,
            "customer": self.customer.pk,
            "delivery": delivery_id,
            "invoice_number": "REPORT-INV-001",
            "invoice_date": "2026-03-10",
            "lines": [{
                "delivery_line": delivery_line_id,
                "unit_price": "100",
                "tax_rate": "14",
            }],
        }, format="json")
        self.assertEqual(invoice.status_code, 201, invoice.data)
        posted_invoice = self.client.post(
            f"/api/sales-documents/invoices/{invoice.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(posted_invoice.status_code, 200, posted_invoice.data)

        sales_return = self.client.post("/api/sales-documents/returns/", {
            "company": self.company.pk,
            "invoice": invoice.data["id"],
            "return_number": "REPORT-RET-001",
            "return_date": "2026-03-12",
            "lines": [{
                "invoice_line": invoice.data["invoice_lines"][0]["id"],
                "quantity": "1",
            }],
        }, format="json")
        self.assertEqual(sales_return.status_code, 201, sales_return.data)
        posted_return = self.client.post(
            f"/api/sales-documents/returns/{sales_return.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(posted_return.status_code, 200, posted_return.data)

        membership = CompanyMembership.objects.get(user=self.user, company=self.company)
        membership.all_branches = False
        membership.save(update_fields=("all_branches",))
        membership.branches.add(self.branch)
        self.branch.is_active = False
        self.branch.save(update_fields=("is_active",))

        report = self.client.get(
            f"/api/sales-documents/invoices/sales-performance/?company={self.company.pk}"
            f"&date_from=2026-03-10&date_to=2026-03-12&branch={self.branch.pk}"
        )
        self.assertEqual(report.status_code, 200, report.data)
        self.assertEqual(report.data["branch"], self.branch.pk)
        self.assertEqual(report.data["totals"]["invoice_count"], 1)
        self.assertEqual(report.data["totals"]["return_count"], 1)
        self.assertEqual(report.data["totals"]["sales_net"], "200.0000")
        self.assertEqual(report.data["totals"]["sales_tax"], "28.0000")
        self.assertEqual(report.data["totals"]["return_net"], "100.0000")
        self.assertEqual(report.data["totals"]["return_tax"], "14.0000")
        self.assertEqual(report.data["totals"]["net_sales"], "100.0000")
        self.assertEqual(report.data["totals"]["net_cost"], "40.0000")
        self.assertEqual(report.data["totals"]["gross_profit"], "60.0000")

        customer_row = report.data["by_customer"][0]
        self.assertEqual(customer_row["customer"], self.customer.pk)
        self.assertEqual(customer_row["sales_net"], "200.0000")
        self.assertEqual(customer_row["return_net"], "100.0000")
        self.assertEqual(customer_row["gross_profit"], "60.0000")

        product_row = report.data["by_product"][0]
        self.assertEqual(product_row["product"], self.product.pk)
        self.assertEqual(product_row["quantity_sold"], "2.0000")
        self.assertEqual(product_row["quantity_returned"], "1.0000")
        self.assertEqual(product_row["net_sales"], "100.0000")
        self.assertEqual(product_row["gross_profit"], "60.0000")

        filtered_report = self.client.get(
            f"/api/sales-documents/invoices/sales-performance/?company={self.company.pk}"
            f"&date_from=2026-03-10&date_to=2026-03-12&customer={self.customer.pk}&product={self.product.pk}"
        )
        self.assertEqual(filtered_report.status_code, 200, filtered_report.data)
        self.assertEqual(filtered_report.data["customer_filter"], self.customer.pk)
        self.assertEqual(filtered_report.data["product_filter"], self.product.pk)
        self.assertEqual(filtered_report.data["totals"]["invoice_count"], 1)
        self.assertEqual(filtered_report.data["totals"]["return_count"], 1)
        self.assertEqual(filtered_report.data["totals"]["sales_net"], "200.0000")
        self.assertEqual(filtered_report.data["totals"]["return_net"], "100.0000")
        self.assertEqual(filtered_report.data["by_customer"][0]["customer"], self.customer.pk)
        self.assertEqual(filtered_report.data["by_product"][0]["product"], self.product.pk)

        invalid_period = self.client.get(
            f"/api/sales-documents/invoices/sales-performance/?company={self.company.pk}"
            "&date_from=2026-03-13&date_to=2026-03-12"
        )
        self.assertEqual(invalid_period.status_code, 400, invalid_period.data)





    def test_sales_performance_report_preserves_return_and_reversal_periods(self):
        delivery_id, delivery_line_id = self._create_delivery("REPORT-REVERSAL-DEL")
        invoice = self.client.post("/api/sales-documents/invoices/", {
            "company": self.company.pk,
            "customer": self.customer.pk,
            "delivery": delivery_id,
            "invoice_number": "REPORT-REVERSAL-INV",
            "invoice_date": "2026-03-10",
            "lines": [{
                "delivery_line": delivery_line_id,
                "unit_price": "100",
                "tax_rate": "14",
            }],
        }, format="json")
        self.assertEqual(invoice.status_code, 201, invoice.data)
        posted_invoice = self.client.post(
            f"/api/sales-documents/invoices/{invoice.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(posted_invoice.status_code, 200, posted_invoice.data)

        sales_return = self.client.post("/api/sales-documents/returns/", {
            "company": self.company.pk,
            "invoice": invoice.data["id"],
            "return_number": "REPORT-REVERSAL-RET",
            "return_date": "2026-03-12",
            "lines": [{
                "invoice_line": invoice.data["invoice_lines"][0]["id"],
                "quantity": "1",
            }],
        }, format="json")
        self.assertEqual(sales_return.status_code, 201, sales_return.data)
        posted_return = self.client.post(
            f"/api/sales-documents/returns/{sales_return.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(posted_return.status_code, 200, posted_return.data)

        reversed_return = self.client.post(
            f"/api/sales-documents/returns/{sales_return.data['id']}/reverse/",
            {"entry_date": "2026-03-14"}, format="json",
        )
        self.assertEqual(reversed_return.status_code, 200, reversed_return.data)

        # The original return remains visible in its historical sales period after reversal.
        # Include the invoice date so net sales/profit reflect both the sale and return.
        original_period = self.client.get(
            f"/api/sales-documents/invoices/sales-performance/?company={self.company.pk}"
            "&date_from=2026-03-10&date_to=2026-03-12"
        )
        self.assertEqual(original_period.status_code, 200, original_period.data)
        self.assertEqual(original_period.data["totals"]["return_count"], 1)
        self.assertEqual(original_period.data["totals"]["return_net"], "100.0000")
        self.assertEqual(original_period.data["totals"]["return_tax"], "14.0000")
        self.assertEqual(original_period.data["totals"]["net_sales"], "100.0000")
        self.assertEqual(original_period.data["totals"]["gross_profit"], "60.0000")
        self.assertEqual(original_period.data["by_product"][0]["quantity_returned"], "1.0000")

        # A return-only date correctly reduces sales and profit for that date.
        return_only_period = self.client.get(
            f"/api/sales-documents/invoices/sales-performance/?company={self.company.pk}"
            "&date_from=2026-03-12&date_to=2026-03-12"
        )
        self.assertEqual(return_only_period.status_code, 200, return_only_period.data)
        self.assertEqual(return_only_period.data["totals"]["net_sales"], "-100.0000")
        self.assertEqual(return_only_period.data["totals"]["net_cost"], "-40.0000")
        self.assertEqual(return_only_period.data["totals"]["gross_profit"], "-60.0000")

        # The reversal is a positive adjustment in its own posting period.
        reversal_period = self.client.get(
            f"/api/sales-documents/invoices/sales-performance/?company={self.company.pk}"
            "&date_from=2026-03-14&date_to=2026-03-14"
        )
        self.assertEqual(reversal_period.status_code, 200, reversal_period.data)
        self.assertEqual(reversal_period.data["totals"]["return_net"], "-100.0000")
        self.assertEqual(reversal_period.data["totals"]["return_tax"], "-14.0000")
        self.assertEqual(reversal_period.data["totals"]["net_sales"], "100.0000")
        self.assertEqual(reversal_period.data["totals"]["net_cost"], "40.0000")
        self.assertEqual(reversal_period.data["totals"]["gross_profit"], "60.0000")
        self.assertEqual(reversal_period.data["by_customer"][0]["quantity_returned"], "-1.0000")
        self.assertEqual(reversal_period.data["by_product"][0]["return_net"], "-100.0000")

        # Both events cancel when the report spans both posting dates.
        combined_period = self.client.get(
            f"/api/sales-documents/invoices/sales-performance/?company={self.company.pk}"
            "&date_from=2026-03-10&date_to=2026-03-14"
        )
        self.assertEqual(combined_period.status_code, 200, combined_period.data)
        self.assertEqual(combined_period.data["totals"]["return_net"], "0.0000")
        self.assertEqual(combined_period.data["totals"]["return_tax"], "0.0000")
        self.assertEqual(combined_period.data["totals"]["net_sales"], "200.0000")
        self.assertEqual(combined_period.data["totals"]["gross_profit"], "120.0000")


    def test_sales_postings_reconcile_to_income_statement_balance_sheet_and_trial_balance(self):
        """Reconcile a posted sale and partial return through the financial statements."""
        delivery_id, delivery_line_id = self._create_delivery("FS-RECON-DEL")
        invoice = self.client.post("/api/sales-documents/invoices/", {
            "company": self.company.pk,
            "customer": self.customer.pk,
            "delivery": delivery_id,
            "invoice_number": "FS-RECON-INV",
            "invoice_date": "2026-03-10",
            "lines": [{
                "delivery_line": delivery_line_id,
                "unit_price": "100",
                "tax_rate": "14",
            }],
        }, format="json")
        self.assertEqual(invoice.status_code, 201, invoice.data)
        posted_invoice = self.client.post(
            f"/api/sales-documents/invoices/{invoice.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(posted_invoice.status_code, 200, posted_invoice.data)

        sales_return = self.client.post("/api/sales-documents/returns/", {
            "company": self.company.pk,
            "invoice": invoice.data["id"],
            "return_number": "FS-RECON-RET",
            "return_date": "2026-03-12",
            "lines": [{
                "invoice_line": invoice.data["invoice_lines"][0]["id"],
                "quantity": "1",
            }],
        }, format="json")
        self.assertEqual(sales_return.status_code, 201, sales_return.data)
        posted_return = self.client.post(
            f"/api/sales-documents/returns/{sales_return.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(posted_return.status_code, 200, posted_return.data)

        income = self.client.get(
            f"/api/accounting/journal-entries/income-statement/?company={self.company.pk}"
            "&date_from=2026-03-01&date_to=2026-03-12"
        )
        self.assertEqual(income.status_code, 200, income.data)
        self.assertEqual(income.data["revenue_total"], Decimal("100.0000"))
        self.assertEqual(income.data["cost_of_sales_total"], Decimal("40.0000"))
        self.assertEqual(income.data["gross_profit"], Decimal("60.0000"))
        self.assertEqual(income.data["expenses_total"], Decimal("0.0000"))
        self.assertEqual(income.data["net_profit"], Decimal("60.0000"))

        balance_sheet = self.client.get(
            f"/api/accounting/journal-entries/balance-sheet/?company={self.company.pk}&as_of=2026-03-12"
        )
        self.assertEqual(balance_sheet.status_code, 200, balance_sheet.data)
        self.assertEqual(balance_sheet.data["total_assets"], Decimal("274.0000"))
        self.assertEqual(balance_sheet.data["total_liabilities"], Decimal("214.0000"))
        self.assertEqual(balance_sheet.data["total_equity"], Decimal("60.0000"))
        self.assertEqual(balance_sheet.data["difference"], Decimal("0.0000"))

        trial_balance = self.client.get(
            f"/api/accounting/journal-entries/trial-balance/?company={self.company.pk}"
            "&date_from=2026-03-01&date_to=2026-03-12"
        )
        self.assertEqual(trial_balance.status_code, 200, trial_balance.data)
        self.assertEqual(trial_balance.data["difference"], Decimal("0.0000"))
        self.assertEqual(
            trial_balance.data["debit_total"], trial_balance.data["credit_total"]
        )

        inventory_account = AccountRole.objects.get(
            company=self.company, role=AccountRole.Role.INVENTORY
        ).account
        receivable_account = AccountRole.objects.get(
            company=self.company, role=AccountRole.Role.ACCOUNTS_RECEIVABLE
        ).account
        grni_account = AccountRole.objects.get(
            company=self.company, role=AccountRole.Role.GOODS_RECEIVED_NOT_INVOICED
        ).account
        inventory_line = balance_sheet.data["assets"]
        balances = {row["account_id"]: row["balance"] for row in inventory_line}
        self.assertEqual(balances[inventory_account.pk], Decimal("160.0000"))
        self.assertEqual(balances[receivable_account.pk], Decimal("114.0000"))
        liabilities = {row["account_id"]: row["balance"] for row in balance_sheet.data["liabilities"]}
        self.assertEqual(liabilities[grni_account.pk], Decimal("200.0000"))

    def test_branch_limited_user_cannot_read_sales_documents_from_other_branch(self):
        from sales.models import SalesDelivery, SalesOrder, SalesReturn

        other_branch = Branch.objects.create(
            company=self.company, code="SALES-PRIVATE", name_ar="فرع خاص",
        )
        other_warehouse = Warehouse.objects.create(
            company=self.company, branch=other_branch,
            code="WH-PRIVATE", name_ar="مخزن الفرع الخاص",
        )
        other_order = SalesOrder.objects.create(
            company=self.company, customer=self.customer, warehouse=other_warehouse,
            order_number="SO-PRIVATE", order_date=date(2026, 3, 10),
            created_by=self.user,
        )
        movement = StockMovement.objects.create(
            company=self.company, movement_number="SD-PRIVATE",
            movement_type=StockMovement.MovementType.ISSUE,
            movement_date=date(2026, 3, 10), source_warehouse=other_warehouse,
            created_by=self.user,
        )
        delivery = SalesDelivery.objects.create(
            company=self.company, customer=self.customer, warehouse=other_warehouse,
            stock_movement=movement, delivery_number="DEL-PRIVATE",
            delivery_date=date(2026, 3, 10), created_by=self.user,
        )
        invoice = SalesInvoice.objects.create(
            company=self.company, customer=self.customer, delivery=delivery,
            invoice_number="INV-PRIVATE", invoice_date=date(2026, 3, 10),
            created_by=self.user,
        )
        receipt = CustomerReceipt.objects.create(
            company=self.company, customer=self.customer, invoice=invoice,
            receipt_number="RC-PRIVATE", receipt_date=date(2026, 3, 10),
            amount=Decimal("10"), created_by=self.user,
        )
        sales_return = SalesReturn.objects.create(
            company=self.company, invoice=invoice, warehouse=other_warehouse,
            branch=other_branch, return_number="RET-PRIVATE",
            return_date=date(2026, 3, 11), created_by=self.user,
        )

        limited_user = get_user_model().objects.create_user(
            username="sales-private-branch-reader", password="test-password",
        )
        membership = CompanyMembership.objects.create(
            user=limited_user, company=self.company, role="accountant",
            all_branches=False,
        )
        membership.branches.add(self.branch)
        self.client.force_authenticate(limited_user)

        documents = (
            ("orders", other_order.pk),
            ("deliveries", delivery.pk),
            ("invoices", invoice.pk),
            ("receipts", receipt.pk),
            ("returns", sales_return.pk),
        )
        for resource, document_id in documents:
            listing = self.client.get(f"/api/sales-documents/{resource}/")
            self.assertEqual(listing.status_code, 200, listing.data)
            rows = listing.data.get("results", listing.data) if isinstance(listing.data, dict) else listing.data
            self.assertNotIn(document_id, [row["id"] for row in rows], resource)
            detail = self.client.get(f"/api/sales-documents/{resource}/{document_id}/")
            self.assertEqual(detail.status_code, 404, (resource, detail.data))

        # Object-scoped actions must not bypass the same branch-filtered queryset.
        self.assertEqual(
            self.client.post(f"/api/sales-documents/invoices/{invoice.pk}/post/", {}, format="json").status_code,
            404,
        )
        self.assertEqual(
            self.client.post(f"/api/sales-documents/receipts/{receipt.pk}/post/", {}, format="json").status_code,
            404,
        )

    def test_sales_documents_from_an_unassigned_company_are_not_readable_by_id(self):
        from sales.models import SalesDelivery

        other_company = Company.objects.create(
            code="SALES-FOREIGN", name_ar="شركة خارج النطاق", country="EG",
            activity="commercial", base_currency=self.company.base_currency,
        )
        other_branch = Branch.objects.create(
            company=other_company, code="MAIN", name_ar="الرئيسي", is_head_office=True,
        )
        other_customer = Customer.objects.create(
            company=other_company, code="FOREIGN-CUS", name_ar="عميل شركة أخرى",
        )
        other_warehouse = Warehouse.objects.create(
            company=other_company, branch=other_branch,
            code="FOREIGN-WH", name_ar="مخزن شركة أخرى",
        )
        movement = StockMovement.objects.create(
            company=other_company, movement_number="FOREIGN-SD",
            movement_type=StockMovement.MovementType.ISSUE,
            movement_date=date(2026, 3, 10), source_warehouse=other_warehouse,
            created_by=self.user,
        )
        delivery = SalesDelivery.objects.create(
            company=other_company, customer=other_customer, warehouse=other_warehouse,
            stock_movement=movement, delivery_number="FOREIGN-DEL",
            delivery_date=date(2026, 3, 10), created_by=self.user,
        )
        invoice = SalesInvoice.objects.create(
            company=other_company, customer=other_customer, delivery=delivery,
            invoice_number="FOREIGN-INV", invoice_date=date(2026, 3, 10),
            created_by=self.user,
        )

        self.assertEqual(
            self.client.get(f"/api/sales-documents/deliveries/{delivery.pk}/").status_code,
            404,
        )
        self.assertEqual(
            self.client.get(f"/api/sales-documents/invoices/{invoice.pk}/").status_code,
            404,
        )
        self.assertEqual(
            self.client.get(
                f"/api/sales-documents/invoices/customer-aging/?company={other_company.pk}"
            ).status_code,
            403,
        )


    def test_partial_invoice_return_uses_original_delivery_unit_cost_in_draft_and_posting(self):
        delivery_id, delivery_line_id = self._create_delivery("RET-PARTIAL-COST-DEL")
        invoice = self.client.post("/api/sales-documents/invoices/", {
            "company": self.company.pk,
            "customer": self.customer.pk,
            "delivery": delivery_id,
            "invoice_number": "RET-PARTIAL-COST-INV",
            "invoice_date": "2026-03-10",
            "lines": [{
                "delivery_line": delivery_line_id,
                "quantity": "1",
                "unit_price": "100",
                "tax_rate": "0",
            }],
        }, format="json")
        self.assertEqual(invoice.status_code, 201, invoice.data)
        posted_invoice = self.client.post(
            f"/api/sales-documents/invoices/{invoice.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(posted_invoice.status_code, 200, posted_invoice.data)

        # The delivery contains two units at 40 each (total cost 80), while
        # this invoice contains only one unit. Returning that unit must carry
        # 40 of cost, not the full delivery-line cost of 80.
        created = self.client.post("/api/sales-documents/returns/", {
            "company": self.company.pk,
            "invoice": invoice.data["id"],
            "return_number": "RET-PARTIAL-COST-001",
            "return_date": "2026-03-11",
            "lines": [{
                "invoice_line": invoice.data["invoice_lines"][0]["id"],
                "quantity": "1",
            }],
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        self.assertEqual(created.data["line_items"][0]["cost_amount"], "40.0000")

        posted_return = self.client.post(
            f"/api/sales-documents/returns/{created.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(posted_return.status_code, 200, posted_return.data)
        self.assertEqual(posted_return.data["line_items"][0]["cost_amount"], "40.0000")
        self.assertEqual(StockBalance.objects.get(
            product=self.product, warehouse=self.warehouse
        ).quantity, Decimal("4.0000"))
