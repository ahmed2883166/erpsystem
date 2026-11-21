from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import connection, DatabaseError, transaction
from rest_framework.test import APITestCase

from accounting.models import AccountRole, ChartOfAccount, JournalEntry, TaxCode
from approvals.models import DiscountApprovalPolicy, SalesInvoiceApproval
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
        second = SalesInvoiceApproval.objects.order_by("-requested_at", "-pk").first()
        self.assertEqual(first.status, SalesInvoiceApproval.Status.REJECTED)
        self.assertEqual(second.status, SalesInvoiceApproval.Status.PENDING)

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

