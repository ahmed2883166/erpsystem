from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import DatabaseError, connection, transaction
from django.db.models import Sum
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.test import APITestCase

from accounting.models import AccountRole, ChartOfAccount, JournalEntry, JournalLine, TaxCode
from approvals.models import DiscountApprovalPolicy, FinancialApprovalRequest
from companies.models import Branch, Company, CompanyMembership, Currency, ExchangeRate, FiscalYear
from inventory.models import Product, StockBalance, StockMovement, StockMovementLine, UnitOfMeasure, Warehouse
from inventory.services import post_stock_movement
from suppliers.models import Supplier
from purchasing.models import PurchaseInvoice, PurchaseInvoiceTaxAllocation, SupplierPayment, SupplierReturn


class PurchasingWorkflowTests(APITestCase):
    def setUp(self):
        currency = Currency.objects.create(code="EGP", name_ar="جنيه مصري")
        self.company = Company.objects.create(code="BUY", name_ar="شركة مشتريات", country="EG", activity="commercial", base_currency=currency)
        self.branch = Branch.objects.create(company=self.company, code="MAIN", name_ar="الرئيسي", is_head_office=True)
        self.year = FiscalYear.objects.create(company=self.company, name="2026", start_date=date(2026, 1, 1), end_date=date(2026, 12, 31))
        self.user = get_user_model().objects.create_user(username="buyer", password="test-password")
        CompanyMembership.objects.create(user=self.user, company=self.company, role="accountant", all_branches=True)
        self.client.force_authenticate(self.user)
        self.supplier = Supplier.objects.create(company=self.company, code="SUP-1", name_ar="المورد الأول")
        self.unit = UnitOfMeasure.objects.create(company=self.company, code="PCS", name_ar="قطعة")
        self.product = Product.objects.create(company=self.company, unit=self.unit, code="P-1", name_ar="صنف 1", purchase_price=Decimal("100"))
        self.warehouse = Warehouse.objects.create(company=self.company, branch=self.branch, code="WH1", name_ar="المخزن الرئيسي")
        roles = {
            AccountRole.Role.INVENTORY: ("1000", "المخزون", ChartOfAccount.Nature.ASSET),
            AccountRole.Role.COGS: ("5100", "تكلفة المبيعات", ChartOfAccount.Nature.EXPENSE),
            AccountRole.Role.INVENTORY_ADJUSTMENT_LOSS: ("5900", "خسائر تسوية المخزون", ChartOfAccount.Nature.EXPENSE),
            AccountRole.Role.INTER_BRANCH_CLEARING: ("2190", "تسوية بين الفروع", ChartOfAccount.Nature.LIABILITY),
            AccountRole.Role.GOODS_RECEIVED_NOT_INVOICED: ("2100", "بضاعة مستلمة غير مفوترة", ChartOfAccount.Nature.LIABILITY),
            AccountRole.Role.ACCOUNTS_PAYABLE: ("2200", "الموردون", ChartOfAccount.Nature.LIABILITY),
            AccountRole.Role.VAT_INPUT: ("1300", "ضريبة مدخلات", ChartOfAccount.Nature.ASSET),
            AccountRole.Role.OPERATING_EXPENSE: ("6000", "مصروفات تشغيلية", ChartOfAccount.Nature.EXPENSE),
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

    def _create_and_post_receipt(self):
        response = self.client.post("/api/purchasing/receipts/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "warehouse": self.warehouse.pk,
            "receipt_number": "GR-001", "receipt_date": "2026-03-10",
            "lines": [{"product": self.product.pk, "quantity": "2", "unit_cost": "100"}],
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        receipt_id = response.data["id"]
        receipt_line_id = response.data["line_items"][0]["id"]
        response = self.client.post(f"/api/purchasing/receipts/{receipt_id}/post/", {}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        return receipt_id, receipt_line_id

    def test_posted_purchase_receipt_update_is_rejected_without_partial_changes(self):
        receipt_id, _ = self._create_and_post_receipt()
        other_supplier = Supplier.objects.create(company=self.company, code="SUP-2", name_ar="مورد آخر")
        response = self.client.patch(
            f"/api/purchasing/receipts/{receipt_id}/",
            {"supplier": other_supplier.pk},
            format="json",
        )
        self.assertEqual(response.status_code, 400, response.data)
        saved = self.client.get(f"/api/purchasing/receipts/{receipt_id}/")
        self.assertEqual(saved.status_code, 200, saved.data)
        self.assertEqual(saved.data["supplier"], self.supplier.pk)

    def test_draft_purchase_order_supports_safe_partial_and_line_updates(self):
        created = self.client.post("/api/purchasing/orders/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "warehouse": self.warehouse.pk,
            "order_number": "PO-EDIT-001", "order_date": "2026-03-10",
            "expected_date": "2026-03-20", "notes": "أمر أولي",
            "lines": [{"product": self.product.pk, "quantity": "5", "unit_price": "100", "tax_rate": "14"}],
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        order_id = created.data["id"]
        updated = self.client.patch(
            f"/api/purchasing/orders/{order_id}/", {"notes": "تم تحديث الملاحظات"}, format="json"
        )
        self.assertEqual(updated.status_code, 200, updated.data)
        self.assertEqual(updated.data["notes"], "تم تحديث الملاحظات")
        revised_lines = self.client.patch(
            f"/api/purchasing/orders/{order_id}/",
            {"lines": [{"product": self.product.pk, "quantity": "7", "unit_price": "105", "tax_rate": "14"}]},
            format="json",
        )
        self.assertEqual(revised_lines.status_code, 200, revised_lines.data)
        self.assertEqual(revised_lines.data["line_items"][0]["quantity"], "7.0000")
        self.assertEqual(revised_lines.data["line_items"][0]["unit_price"], "105.0000")

    def test_receipt_posting_rejects_inactive_supplier_and_preserves_stock(self):
        created = self.client.post("/api/purchasing/receipts/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "warehouse": self.warehouse.pk,
            "receipt_number": "GR-INACTIVE-SUP", "receipt_date": "2026-03-10",
            "lines": [{"product": self.product.pk, "quantity": "2", "unit_cost": "100"}],
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        self.supplier.is_active = False
        self.supplier.save(update_fields=("is_active",))
        rejected = self.client.post(
            f"/api/purchasing/receipts/{created.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(rejected.status_code, 400, rejected.data)
        self.assertFalse(StockBalance.objects.filter(product=self.product, warehouse=self.warehouse).exists())
        self.assertFalse(JournalEntry.objects.filter(source_type=JournalEntry.Source.INVENTORY).exists())

    def test_receipt_posts_inventory_and_grni_once_then_invoice_clears_grni(self):
        receipt_id, receipt_line_id = self._create_and_post_receipt()
        balance = StockBalance.objects.get(product=self.product, warehouse=self.warehouse)
        self.assertEqual(balance.quantity, Decimal("2"))
        self.assertEqual(balance.inventory_value, Decimal("200.0000"))
        self.assertEqual(JournalEntry.objects.filter(source_type=JournalEntry.Source.INVENTORY, status="posted").count(), 1)

        response = self.client.post("/api/purchasing/invoices/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "receipt": receipt_id,
            "invoice_number": "INV-S-001", "invoice_date": "2026-03-10",
            "lines": [{"receipt_line": receipt_line_id, "unit_price": "100", "tax_rate": "14"}],
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        invoice_id = response.data["id"]
        response = self.client.post(f"/api/purchasing/invoices/{invoice_id}/post/", {}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["net_total"], "200.0000")
        self.assertEqual(response.data["tax_total"], "28.0000")
        self.assertEqual(response.data["grand_total"], "228.0000")
        self.assertEqual(StockBalance.objects.get(product=self.product, warehouse=self.warehouse).quantity, Decimal("2"))
        self.assertEqual(JournalEntry.objects.filter(source_type=JournalEntry.Source.PURCHASES, status="posted").count(), 1)
        invoice_entry_id = response.data["journal_entry"]
        purchase_lines = list(JournalLine.objects.filter(entry_id=invoice_entry_id).select_related("account"))
        self.assertEqual(sum((line.debit for line in purchase_lines), Decimal("0")), Decimal("228.0000"))
        self.assertEqual(sum((line.credit for line in purchase_lines), Decimal("0")), Decimal("228.0000"))
        by_code = {line.account.code: line for line in purchase_lines}
        self.assertEqual(by_code["2100"].debit, Decimal("200.0000"))
        self.assertEqual(by_code["2100"].credit, Decimal("0"))
        self.assertEqual(by_code["1300"].debit, Decimal("28.0000"))
        self.assertEqual(by_code["2200"].credit, Decimal("228.0000"))
        self.assertEqual(by_code["2100"].debit-by_code["2100"].credit, Decimal("200.0000"))

    def test_reposting_receipt_or_invoice_does_not_duplicate_stock_or_journals(self):
        receipt_id, receipt_line_id = self._create_and_post_receipt()
        first_balance = StockBalance.objects.get(product=self.product, warehouse=self.warehouse)
        self.assertEqual(first_balance.quantity, Decimal("2"))
        duplicate_receipt = self.client.post(
            f"/api/purchasing/receipts/{receipt_id}/post/", {}, format="json"
        )
        self.assertEqual(duplicate_receipt.status_code, 400)

        invoice_response = self.client.post("/api/purchasing/invoices/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "receipt": receipt_id,
            "invoice_number": "INV-RETRY", "invoice_date": "2026-03-10",
            "lines": [{"receipt_line": receipt_line_id, "unit_price": "100", "tax_rate": "14"}],
        }, format="json")
        self.assertEqual(invoice_response.status_code, 201, invoice_response.data)
        invoice_id = invoice_response.data["id"]
        first_post = self.client.post(
            f"/api/purchasing/invoices/{invoice_id}/post/", {}, format="json"
        )
        self.assertEqual(first_post.status_code, 200, first_post.data)
        duplicate_invoice = self.client.post(
            f"/api/purchasing/invoices/{invoice_id}/post/", {}, format="json"
        )
        self.assertEqual(duplicate_invoice.status_code, 400)
        self.assertEqual(
            StockBalance.objects.get(product=self.product, warehouse=self.warehouse).quantity,
            Decimal("2"),
        )
        self.assertEqual(JournalEntry.objects.filter(source_type=JournalEntry.Source.INVENTORY).count(), 1)
        self.assertEqual(JournalEntry.objects.filter(source_type=JournalEntry.Source.PURCHASES).count(), 1)

    def test_supplier_payment_reduces_invoice_balance_and_blocks_overpayment(self):
        receipt_id, receipt_line_id = self._create_and_post_receipt()
        invoice_response = self.client.post("/api/purchasing/invoices/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "receipt": receipt_id,
            "invoice_number": "INV-PAY-001", "invoice_date": "2026-03-10",
            "lines": [{"receipt_line": receipt_line_id, "unit_price": "100", "tax_rate": "14"}],
        }, format="json")
        self.assertEqual(invoice_response.status_code, 201, invoice_response.data)
        invoice_id = invoice_response.data["id"]
        posted_invoice = self.client.post(
            f"/api/purchasing/invoices/{invoice_id}/post/", {}, format="json"
        )
        self.assertEqual(posted_invoice.status_code, 200, posted_invoice.data)

        alternate_bank = ChartOfAccount.objects.create(
            company=self.company, code="1110", name_ar="البنك الفرعي", nature=ChartOfAccount.Nature.ASSET
        )
        from treasury.models import TreasuryAccount
        TreasuryAccount.objects.create(company=self.company, branch=self.branch, account=alternate_bank,
            kind=TreasuryAccount.Kind.BANK, code="BANK-2", name_ar="البنك الفرعي", currency_code="EGP")
        payment_response = self.client.post("/api/purchasing/payments/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "invoice": invoice_id,
            "payment_number": "PAY-001", "payment_date": "2026-03-15",
            "method": "bank", "payment_account": alternate_bank.pk,
            "amount": "100.0000", "reference": "BANK-REF-1",
        }, format="json")
        self.assertEqual(payment_response.status_code, 201, payment_response.data)
        payment_id = payment_response.data["id"]
        posted_payment = self.client.post(
            f"/api/purchasing/payments/{payment_id}/post/", {}, format="json"
        )
        self.assertEqual(posted_payment.status_code, 200, posted_payment.data)
        self.assertEqual(posted_payment.data["payment_account"], alternate_bank.pk)
        self.assertTrue(JournalLine.objects.filter(
            entry_id=posted_payment.data["journal_entry"], account=alternate_bank,
            credit=Decimal("100.0000"),
        ).exists())
        retry_payment = self.client.post(
            f"/api/purchasing/payments/{payment_id}/post/", {}, format="json"
        )
        self.assertEqual(retry_payment.status_code, 400)
        self.assertEqual(
            JournalEntry.objects.filter(source_type=JournalEntry.Source.PURCHASES, status="posted").count(),
            2,
        )
        invoice_data = self.client.get(f"/api/purchasing/invoices/{invoice_id}/").data
        self.assertEqual(invoice_data["paid_total"], "100.0000")
        self.assertEqual(invoice_data["balance_due"], "128.0000")

        overpayment = self.client.post("/api/purchasing/payments/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "invoice": invoice_id,
            "payment_number": "PAY-002", "payment_date": "2026-03-16",
            "method": "bank", "amount": "129.0000",
        }, format="json")
        self.assertEqual(overpayment.status_code, 201, overpayment.data)
        rejected = self.client.post(
            f"/api/purchasing/payments/{overpayment.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(
            JournalEntry.objects.filter(source_type=JournalEntry.Source.PURCHASES, status="posted").count(),
            2,
        )

    def test_supplier_payment_rejects_treasury_account_from_another_branch(self):
        receipt_id, receipt_line_id = self._create_and_post_receipt()
        invoice = self.client.post("/api/purchasing/invoices/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "receipt": receipt_id,
            "invoice_number": "INV-BRANCH-PAY", "invoice_date": "2026-03-10",
            "lines": [{"receipt_line": receipt_line_id, "unit_price": "100", "tax_rate": "14"}],
        }, format="json")
        self.assertEqual(invoice.status_code, 201, invoice.data)
        posted_invoice = self.client.post(
            f"/api/purchasing/invoices/{invoice.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(posted_invoice.status_code, 200, posted_invoice.data)

        other_branch = Branch.objects.create(company=self.company, code="BR2", name_ar="فرع آخر")
        other_bank = ChartOfAccount.objects.create(
            company=self.company, code="1120", name_ar="بنك الفرع الآخر", nature=ChartOfAccount.Nature.ASSET
        )
        from treasury.models import TreasuryAccount
        TreasuryAccount.objects.create(
            company=self.company, branch=other_branch, account=other_bank,
            kind=TreasuryAccount.Kind.BANK, code="BANK-BR2", name_ar="بنك الفرع الآخر",
            currency_code="EGP",
        )
        payment = SupplierPayment.objects.create(
            company=self.company, supplier=self.supplier,
            invoice_id=invoice.data["id"], payment_number="PAY-WRONG-BRANCH",
            payment_date=date(2026, 3, 15), method=SupplierPayment.Method.BANK,
            payment_account=other_bank, amount=Decimal("25.0000"),
        )

        from purchasing.services import post_supplier_payment
        with self.assertRaises(DRFValidationError):
            post_supplier_payment(payment.pk, self.user)
        payment.refresh_from_db()
        self.assertEqual(payment.status, SupplierPayment.Status.DRAFT)
        self.assertIsNone(payment.journal_entry_id)
        self.assertEqual(
            JournalEntry.objects.filter(
                source_type=JournalEntry.Source.PURCHASES, status=JournalEntry.Status.POSTED
            ).count(),
            1,
        )

    def test_supplier_statement_reports_opening_balance_and_running_payable(self):
        receipt_id, receipt_line_id = self._create_and_post_receipt()
        invoice_response = self.client.post("/api/purchasing/invoices/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "receipt": receipt_id,
            "invoice_number": "INV-STAT-001", "invoice_date": "2026-03-10",
            "lines": [{"receipt_line": receipt_line_id, "unit_price": "100", "tax_rate": "14"}],
        }, format="json")
        self.assertEqual(invoice_response.status_code, 201, invoice_response.data)
        invoice_id = invoice_response.data["id"]
        posted_invoice = self.client.post(
            f"/api/purchasing/invoices/{invoice_id}/post/", {}, format="json"
        )
        self.assertEqual(posted_invoice.status_code, 200, posted_invoice.data)

        payment_response = self.client.post("/api/purchasing/payments/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "invoice": invoice_id,
            "payment_number": "PAY-STAT-001", "payment_date": "2026-03-18",
            "method": "bank", "amount": "50.0000",
        }, format="json")
        self.assertEqual(payment_response.status_code, 201, payment_response.data)
        posted_payment = self.client.post(
            f"/api/purchasing/payments/{payment_response.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(posted_payment.status_code, 200, posted_payment.data)

        statement = self.client.get(
            "/api/purchasing/invoices/supplier-statement/",
            {"company": self.company.pk, "supplier": self.supplier.pk,
             "date_from": "2026-03-15", "date_to": "2026-03-31"},
        )
        self.assertEqual(statement.status_code, 200, statement.data)
        self.assertEqual(statement.data["opening_balance"], "228.0000")
        self.assertEqual(statement.data["closing_balance"], "178.0000")
        self.assertEqual(len(statement.data["rows"]), 1)
        self.assertEqual(statement.data["rows"][0]["document_type"], "payment")
        self.assertEqual(statement.data["rows"][0]["debit"], "50.0000")

    def test_supplier_aging_groups_open_balance_by_days_past_due(self):
        self.supplier.payment_terms_days = 0
        self.supplier.save(update_fields=["payment_terms_days"])
        receipt_id, receipt_line_id = self._create_and_post_receipt()
        invoice = self.client.post("/api/purchasing/invoices/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "receipt": receipt_id,
            "invoice_number": "INV-AGE-001", "invoice_date": "2026-03-10",
            "lines": [{"receipt_line": receipt_line_id, "unit_price": "100", "tax_rate": "14"}],
        }, format="json")
        self.assertEqual(invoice.status_code, 201, invoice.data)
        posted = self.client.post(f"/api/purchasing/invoices/{invoice.data['id']}/post/", {}, format="json")
        self.assertEqual(posted.status_code, 200, posted.data)
        payment = self.client.post("/api/purchasing/payments/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "invoice": invoice.data["id"],
            "payment_number": "PAY-AGE-001", "payment_date": "2026-03-20",
            "method": "bank", "amount": "50.0000",
        }, format="json")
        self.assertEqual(payment.status_code, 201, payment.data)
        posted_payment = self.client.post(f"/api/purchasing/payments/{payment.data['id']}/post/", {}, format="json")
        self.assertEqual(posted_payment.status_code, 200, posted_payment.data)

        historical_aging = self.client.get(
            "/api/purchasing/invoices/supplier-aging/",
            {"company": self.company.pk, "as_of": "2026-03-15"},
        )
        self.assertEqual(historical_aging.status_code, 200, historical_aging.data)
        self.assertEqual(historical_aging.data["totals"]["total_due"], "228.0000")

        aging = self.client.get(
            "/api/purchasing/invoices/supplier-aging/",
            {"company": self.company.pk, "as_of": "2026-03-25"},
        )
        self.assertEqual(aging.status_code, 200, aging.data)
        self.assertEqual(aging.data["totals"]["1_30"], "178.0000")
        self.assertEqual(aging.data["totals"]["total_due"], "178.0000")
        self.assertEqual(aging.data["suppliers"][0]["invoices"][0]["overdue_days"], 15)

        branch_aging = self.client.get(
            "/api/purchasing/invoices/supplier-aging/",
            {"company": self.company.pk, "as_of": "2026-03-25", "branch": self.branch.pk},
        )
        self.assertEqual(branch_aging.status_code, 200, branch_aging.data)
        self.assertEqual(branch_aging.data["totals"]["total_due"], "178.0000")

        forbidden_branch = self.client.get(
            "/api/purchasing/invoices/supplier-aging/",
            {"company": self.company.pk, "as_of": "2026-03-25", "branch": 999999},
        )
        self.assertEqual(forbidden_branch.status_code, 403)

    def test_purchase_order_supports_partial_receipts_and_blocks_over_receipt(self):
        order_response = self.client.post("/api/purchasing/orders/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "warehouse": self.warehouse.pk,
            "order_number": "PO-001", "order_date": "2026-03-01", "expected_date": "2026-03-20",
            "lines": [{"product": self.product.pk, "quantity": "10", "unit_price": "100", "tax_rate": "14"}],
        }, format="json")
        self.assertEqual(order_response.status_code, 201, order_response.data)
        order_id = order_response.data["id"]
        order_line_id = order_response.data["line_items"][0]["id"]
        confirmed = self.client.post(f"/api/purchasing/orders/{order_id}/confirm/", {}, format="json")
        self.assertEqual(confirmed.status_code, 200, confirmed.data)
        self.assertEqual(confirmed.data["status"], "confirmed")

        receipt = self.client.post("/api/purchasing/receipts/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "warehouse": self.warehouse.pk,
            "purchase_order": order_id, "receipt_number": "GR-PO-001", "receipt_date": "2026-03-05",
            "lines": [{"product": self.product.pk, "quantity": "4", "unit_cost": "100",
                       "purchase_order_line": order_line_id}],
        }, format="json")
        self.assertEqual(receipt.status_code, 201, receipt.data)
        posted = self.client.post(f"/api/purchasing/receipts/{receipt.data['id']}/post/", {}, format="json")
        self.assertEqual(posted.status_code, 200, posted.data)
        order = self.client.get(f"/api/purchasing/orders/{order_id}/").data
        self.assertEqual(order["status"], "partially_received")
        self.assertEqual(order["line_items"][0]["received_quantity"], "4.0000")
        self.assertEqual(order["line_items"][0]["remaining_quantity"], "6.0000")

        over_receipt = self.client.post("/api/purchasing/receipts/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "warehouse": self.warehouse.pk,
            "purchase_order": order_id, "receipt_number": "GR-PO-002", "receipt_date": "2026-03-06",
            "lines": [{"product": self.product.pk, "quantity": "7", "unit_cost": "100",
                       "purchase_order_line": order_line_id}],
        }, format="json")
        self.assertEqual(over_receipt.status_code, 400, over_receipt.data)

        second_receipt = self.client.post("/api/purchasing/receipts/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "warehouse": self.warehouse.pk,
            "purchase_order": order_id, "receipt_number": "GR-PO-003", "receipt_date": "2026-03-07",
            "lines": [{"product": self.product.pk, "quantity": "6", "unit_cost": "100",
                       "purchase_order_line": order_line_id}],
        }, format="json")
        self.assertEqual(second_receipt.status_code, 201, second_receipt.data)
        second_posted = self.client.post(f"/api/purchasing/receipts/{second_receipt.data['id']}/post/", {}, format="json")
        self.assertEqual(second_posted.status_code, 200, second_posted.data)
        order = self.client.get(f"/api/purchasing/orders/{order_id}/").data
        self.assertEqual(order["status"], "received")
        self.assertEqual(order["line_items"][0]["received_quantity"], "10.0000")

    def test_invoice_rejects_price_mismatch_against_grni(self):
        receipt_id, receipt_line_id = self._create_and_post_receipt()
        response = self.client.post("/api/purchasing/invoices/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "receipt": receipt_id,
            "invoice_number": "INV-S-002", "invoice_date": "2026-03-10",
            "lines": [{"receipt_line": receipt_line_id, "unit_price": "95", "tax_rate": "14"}],
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        invoice_id = response.data["id"]
        response = self.client.post(f"/api/purchasing/invoices/{invoice_id}/post/", {}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(StockBalance.objects.get(product=self.product, warehouse=self.warehouse).quantity, Decimal("2"))
        self.assertFalse(JournalEntry.objects.filter(source_type=JournalEntry.Source.PURCHASES).exists())

        # A draft mismatch remains correctable; the corrected invoice can clear GRNI.
        corrected = self.client.patch(
            f"/api/purchasing/invoices/{invoice_id}/",
            {"lines": [{"receipt_line": receipt_line_id, "unit_price": "100", "tax_rate": "14"}]},
            format="json",
        )
        self.assertEqual(corrected.status_code, 200, corrected.data)
        self.assertEqual(corrected.data["net_total"], "200.0000")
        posted = self.client.post(f"/api/purchasing/invoices/{invoice_id}/post/", {}, format="json")
        self.assertEqual(posted.status_code, 200, posted.data)
        self.assertEqual(posted.data["status"], "posted")
        self.assertEqual(StockBalance.objects.get(product=self.product, warehouse=self.warehouse).quantity, Decimal("2"))
        self.assertTrue(JournalEntry.objects.filter(source_type=JournalEntry.Source.PURCHASES, status="posted").exists())

    def test_invoice_cannot_be_created_before_receipt_is_posted(self):
        response = self.client.post("/api/purchasing/receipts/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "warehouse": self.warehouse.pk,
            "receipt_number": "GR-002", "receipt_date": "2026-03-11",
            "lines": [{"product": self.product.pk, "quantity": "1", "unit_cost": "100"}],
        }, format="json")
        self.assertEqual(response.status_code, 201)
        response = self.client.post("/api/purchasing/invoices/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "receipt": response.data["id"],
            "invoice_number": "INV-S-003", "invoice_date": "2026-03-11",
            "lines": [{"receipt_line": 2, "unit_price": "100", "tax_rate": "0"}],
        }, format="json")
        self.assertEqual(response.status_code, 400)

    def test_viewer_cannot_post_receipt(self):
        receipt_response = self.client.post("/api/purchasing/receipts/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "warehouse": self.warehouse.pk,
            "receipt_number": "GR-003", "receipt_date": "2026-03-12",
            "lines": [{"product": self.product.pk, "quantity": "1", "unit_cost": "100"}],
        }, format="json")
        self.assertEqual(receipt_response.status_code, 201)
        viewer = get_user_model().objects.create_user(username="buyer_viewer", password="test-password")
        CompanyMembership.objects.create(user=viewer, company=self.company, role="viewer", all_branches=True)
        self.client.force_authenticate(viewer)
        response = self.client.post(f"/api/purchasing/receipts/{receipt_response.data['id']}/post/", {}, format="json")
        self.assertEqual(response.status_code, 403)


    def test_purchase_invoice_uses_configured_recoverable_input_tax_code(self):
        receipt_id, receipt_line_id = self._create_and_post_receipt()
        code = TaxCode.objects.create(
            company=self.company, country="EG", code="INPUT-14", name_ar="ضريبة مدخلات",
            category=TaxCode.Category.STANDARD, rate=Decimal("14"),
            effective_from=date(2026, 1, 1), input_tax_recoverable=True,
        )
        response = self.client.post("/api/purchasing/invoices/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "receipt": receipt_id,
            "invoice_number": "INV-TAX-IN-1", "invoice_date": "2026-03-10",
            "lines": [{"receipt_line": receipt_line_id, "unit_price": "100", "tax_code": code.pk}],
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["invoice_lines"][0]["tax_rate"], "14.0000")
        self.assertEqual(response.data["invoice_lines"][0]["tax_code"], code.pk)
        self.assertEqual(response.data["tax_total"], "28.0000")
        posted = self.client.post(f"/api/purchasing/invoices/{response.data['id']}/post/", {}, format="json")
        self.assertEqual(posted.status_code, 200, posted.data)
        report = self.client.get(
            f"/api/accounting/journal-entries/vat-return/?company={self.company.pk}&date_from=2026-03-01&date_to=2026-03-31"
        )
        self.assertEqual(report.status_code, 200, report.data)
        self.assertEqual(report.data["input_tax"], "28.0000")
        self.assertEqual(Decimal(report.data["ledger_reconciliation"]["input_tax_difference"]), Decimal("0"))
        self.assertEqual(report.data["purchases"]["by_code"][0]["tax_code"], "INPUT-14")
        self.assertEqual(report.data["purchases"]["by_category"][0]["category"], "standard")

    def test_legacy_purchase_tax_without_tax_code_matches_historical_vat_input_posting(self):
        receipt_id, receipt_line_id = self._create_and_post_receipt()
        created = self.client.post("/api/purchasing/invoices/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "receipt": receipt_id,
            "invoice_number": "INV-LEGACY-TAX-01", "invoice_date": "2026-03-10",
            "lines": [{"receipt_line": receipt_line_id, "unit_price": "100", "tax_rate": "14"}],
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        posted = self.client.post(
            f"/api/purchasing/invoices/{created.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(posted.status_code, 200, posted.data)
        report = self.client.get(
            f"/api/accounting/journal-entries/vat-return/?company={self.company.pk}&date_from=2026-03-01&date_to=2026-03-31"
        )
        self.assertEqual(report.status_code, 200, report.data)
        self.assertEqual(report.data["input_tax"], "28.0000")
        self.assertEqual(report.data["purchases"]["recoverable_tax_amount"], "28.0000")
        self.assertEqual(report.data["purchases"]["nonrecoverable_tax_amount"], "0.0000")
        self.assertEqual(report.data["unclassified_tax"]["purchase_amount"], "28.0000")
        self.assertEqual(report.data["unclassified_tax"]["purchase_recoverable_amount"], "28.0000")
        self.assertEqual(
            Decimal(report.data["ledger_reconciliation"]["input_tax_difference"]), Decimal("0")
        )

    def test_nonrecoverable_purchase_tax_is_split_between_remaining_stock_and_consumed_cogs_by_branch(self):
        receipt_id, receipt_line_id = self._create_and_post_receipt()
        second_branch = Branch.objects.create(
            company=self.company, code="BR-02", name_ar="الفرع الثاني",
        )
        second_warehouse = Warehouse.objects.create(
            company=self.company, branch=second_branch, code="WH2", name_ar="مخزن الفرع الثاني",
        )
        transfer = StockMovement.objects.create(
            company=self.company, movement_number="TAX-XFER-01",
            movement_type=StockMovement.MovementType.TRANSFER, movement_date=date(2026, 3, 11),
            source_warehouse=self.warehouse, destination_warehouse=second_warehouse,
            created_by=self.user,
        )
        StockMovementLine.objects.create(
            movement=transfer, product=self.product, quantity=Decimal("1"),
        )
        post_stock_movement(transfer.pk, self.user)

        issue = StockMovement.objects.create(
            company=self.company, movement_number="TAX-ISS-01",
            movement_type=StockMovement.MovementType.ISSUE, movement_date=date(2026, 3, 12),
            source_warehouse=second_warehouse, created_by=self.user,
        )
        StockMovementLine.objects.create(
            movement=issue, product=self.product, quantity=Decimal("1"),
        )
        post_stock_movement(issue.pk, self.user)

        code = TaxCode.objects.create(
            company=self.company, country="EG", code="INPUT-NR", name_ar="ضريبة غير قابلة للخصم",
            category=TaxCode.Category.STANDARD, rate=Decimal("14"),
            effective_from=date(2026, 1, 1), input_tax_recoverable=False,
        )
        created = self.client.post("/api/purchasing/invoices/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "receipt": receipt_id,
            "invoice_number": "INV-TAX-IN-2", "invoice_date": "2026-03-13",
            "lines": [{"receipt_line": receipt_line_id, "unit_price": "100", "tax_code": code.pk}],
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        line_data = created.data["invoice_lines"][0]
        self.assertEqual(line_data["recoverable_tax_amount"], "0.0000")
        self.assertEqual(line_data["nonrecoverable_tax_amount"], "28.0000")

        posted = self.client.post(f"/api/purchasing/invoices/{created.data['id']}/post/", {}, format="json")
        self.assertEqual(posted.status_code, 200, posted.data)
        invoice = PurchaseInvoice.objects.get(pk=created.data["id"])
        self.assertEqual(invoice.grand_total, Decimal("228.0000"))
        vat_report = self.client.get(
            f"/api/accounting/journal-entries/vat-return/?company={self.company.pk}&date_from=2026-03-01&date_to=2026-03-31"
        )
        self.assertEqual(vat_report.status_code, 200, vat_report.data)
        self.assertEqual(vat_report.data["purchases"]["tax_amount"], "28.0000")
        self.assertEqual(vat_report.data["purchases"]["recoverable_tax_amount"], "0")
        self.assertEqual(vat_report.data["purchases"]["nonrecoverable_tax_amount"], "28.0000")
        self.assertEqual(vat_report.data["purchases"]["by_code"][0]["nonrecoverable_tax_amount"], "28.0000")
        self.assertEqual(vat_report.data["input_tax"], "0")
        self.assertEqual(Decimal(vat_report.data["ledger_reconciliation"]["input_tax_difference"]), Decimal("0"))
        self.assertEqual(invoice.journal_entry.lines.get(
            account=AccountRole.objects.get(company=self.company, role=AccountRole.Role.ACCOUNTS_PAYABLE).account
        ).credit, Decimal("228.0000"))
        self.assertFalse(invoice.journal_entry.lines.filter(
            account=AccountRole.objects.get(company=self.company, role=AccountRole.Role.VAT_INPUT).account,
            debit__gt=0,
        ).exists())

        source_balance = StockBalance.objects.get(product=self.product, warehouse=self.warehouse)
        destination_balance = StockBalance.objects.get(product=self.product, warehouse=second_warehouse)
        self.assertEqual(source_balance.quantity, Decimal("1"))
        self.assertEqual(source_balance.inventory_value, Decimal("114.0000"))
        self.assertEqual(destination_balance.quantity, Decimal("0"))
        allocations = list(PurchaseInvoiceTaxAllocation.objects.filter(invoice=invoice).select_related("branch", "journal_entry"))
        self.assertEqual(len(allocations), 2)
        by_branch = {row.branch_id: row for row in allocations}
        self.assertEqual(by_branch[self.branch.pk].inventory_amount, Decimal("14.0000"))
        self.assertEqual(by_branch[self.branch.pk].cogs_amount, Decimal("0.0000"))
        self.assertEqual(by_branch[second_branch.pk].inventory_amount, Decimal("0.0000"))
        self.assertEqual(by_branch[second_branch.pk].cogs_amount, Decimal("14.0000"))
        self.assertEqual(len(posted.data["tax_allocations"]), 2)
        self.assertEqual(
            {row["branch"]: row["inventory_amount"] for row in posted.data["tax_allocations"]},
            {self.branch.pk: "14.0000", second_branch.pk: "0.0000"},
        )
        self.assertEqual(
            {row["branch"]: row["cogs_amount"] for row in posted.data["tax_allocations"]},
            {self.branch.pk: "0.0000", second_branch.pk: "14.0000"},
        )
        self.assertEqual(
            by_branch[second_branch.pk].journal_entry.lines.get(
                account=AccountRole.objects.get(company=self.company, role=AccountRole.Role.COGS).account
            ).debit,
            Decimal("14.0000"),
        )
        clearing = AccountRole.objects.get(company=self.company, role=AccountRole.Role.INTER_BRANCH_CLEARING).account
        allocation_entries = [invoice.journal_entry, *(row.journal_entry for row in allocations)]
        clearing_debit = sum(
            (entry.lines.filter(account=clearing).aggregate(total=Sum("debit"))["total"] or Decimal("0"))
            for entry in allocation_entries
        )
        clearing_credit = sum(
            (entry.lines.filter(account=clearing).aggregate(total=Sum("credit"))["total"] or Decimal("0"))
            for entry in allocation_entries
        )
        self.assertEqual(clearing_debit, Decimal("28.0000"))
        self.assertEqual(clearing_credit, Decimal("28.0000"))


    def test_nonrecoverable_purchase_tax_revalues_fifo_layers_and_remaining_inventory(self):
        self.product.costing_method = Product.CostingMethod.FIFO
        self.product.save(update_fields=("costing_method",))
        receipt_id, receipt_line_id = self._create_and_post_receipt()
        code = TaxCode.objects.create(
            company=self.company, country="EG", code="FIFO-INPUT-NR",
            name_ar="ضريبة غير قابلة للخصم - FIFO",
            category=TaxCode.Category.STANDARD, rate=Decimal("14"),
            effective_from=date(2026, 1, 1), input_tax_recoverable=False,
        )
        created = self.client.post("/api/purchasing/invoices/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "receipt": receipt_id,
            "invoice_number": "INV-FIFO-NR-01", "invoice_date": "2026-03-10",
            "lines": [{"receipt_line": receipt_line_id, "unit_price": "100", "tax_code": code.pk}],
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        posted = self.client.post(f"/api/purchasing/invoices/{created.data['id']}/post/", {}, format="json")
        self.assertEqual(posted.status_code, 200, posted.data)
        balance = StockBalance.objects.get(product=self.product, warehouse=self.warehouse)
        layer = self.product.cost_layers.get(root_source_line_id=receipt_line_id)
        self.assertEqual(balance.inventory_value, Decimal("228.0000"))
        self.assertEqual(balance.average_cost, Decimal("114.000000"))
        self.assertEqual(layer.unit_cost, Decimal("114.00000000"))
        allocation = PurchaseInvoiceTaxAllocation.objects.get(invoice_id=created.data["id"])
        self.assertEqual(allocation.inventory_amount, Decimal("28.0000"))
        self.assertEqual(allocation.cogs_amount, Decimal("0.0000"))

    def test_nonrecoverable_purchase_tax_on_adjusted_stock_is_allocated_to_inventory_loss(self):
        receipt_id, receipt_line_id = self._create_and_post_receipt()
        adjustment = StockMovement.objects.create(
            company=self.company, movement_number="TAX-ADJ-OUT-01",
            movement_type=StockMovement.MovementType.ADJUSTMENT, movement_date=date(2026, 3, 11),
            source_warehouse=self.warehouse, created_by=self.user,
        )
        StockMovementLine.objects.create(
            movement=adjustment, product=self.product, quantity=Decimal("1"),
            adjustment_direction=StockMovementLine.AdjustmentDirection.OUT,
        )
        post_stock_movement(adjustment.pk, self.user)
        code = TaxCode.objects.create(
            company=self.company, country="EG", code="ADJ-INPUT-NR",
            name_ar="ضريبة غير قابلة للخصم - تسوية مخزون",
            category=TaxCode.Category.STANDARD, rate=Decimal("14"),
            effective_from=date(2026, 1, 1), input_tax_recoverable=False,
        )
        created = self.client.post("/api/purchasing/invoices/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "receipt": receipt_id,
            "invoice_number": "INV-ADJ-NR-01", "invoice_date": "2026-03-12",
            "lines": [{"receipt_line": receipt_line_id, "unit_price": "100", "tax_code": code.pk}],
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        posted = self.client.post(f"/api/purchasing/invoices/{created.data['id']}/post/", {}, format="json")
        self.assertEqual(posted.status_code, 200, posted.data)
        allocation = PurchaseInvoiceTaxAllocation.objects.get(invoice_id=created.data["id"])
        self.assertEqual(allocation.inventory_amount, Decimal("14.0000"))
        self.assertEqual(allocation.adjustment_loss_amount, Decimal("14.0000"))
        loss_account = AccountRole.objects.get(
            company=self.company, role=AccountRole.Role.INVENTORY_ADJUSTMENT_LOSS,
        ).account
        self.assertEqual(
            allocation.journal_entry.lines.get(account=loss_account).debit,
            Decimal("14.0000"),
        )

    def test_purchase_invoice_over_limit_requires_manager_approval_of_exact_snapshot(self):
        DiscountApprovalPolicy.objects.create(
            company=self.company, max_purchase_invoice_amount=Decimal("200"),
            created_by=self.user, updated_by=self.user,
        )
        receipt_id, receipt_line_id = self._create_and_post_receipt()
        invoice = self.client.post("/api/purchasing/invoices/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "receipt": receipt_id,
            "invoice_number": "INV-APPROVAL-001", "invoice_date": "2026-03-10",
            "lines": [{"receipt_line": receipt_line_id, "unit_price": "100", "tax_rate": "14"}],
        }, format="json")
        self.assertEqual(invoice.status_code, 201, invoice.data)
        request = FinancialApprovalRequest.objects.get(
            purchase_invoice_id=invoice.data["id"]
        )
        self.assertEqual(request.requested_amount, Decimal("228.0000"))
        self.assertEqual(request.requested_limit, Decimal("200.0000"))
        with self.assertRaises(DatabaseError):
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(
                        "UPDATE approvals_financialapprovalrequest SET requested_amount = %s WHERE id = %s",
                        ["1", request.pk],
                    )
        request.refresh_from_db()
        self.assertEqual(request.requested_amount, Decimal("228.0000"))
        blocked = self.client.post(
            f"/api/purchasing/invoices/{invoice.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(blocked.status_code, 400, blocked.data)

        manager = get_user_model().objects.create_user(username="purchase-approval-admin", password="test-password")
        CompanyMembership.objects.create(user=manager, company=self.company, role="admin", all_branches=True)
        self.client.force_authenticate(manager)
        approved = self.client.post(
            f"/api/approvals/financial-requests/{request.pk}/approve/",
            {"note": "اعتماد فاتورة مشتريات"}, format="json",
        )
        self.assertEqual(approved.status_code, 200, approved.data)
        self.client.force_authenticate(self.user)
        posted = self.client.post(
            f"/api/purchasing/invoices/{invoice.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(posted.status_code, 200, posted.data)

    def test_supplier_payment_over_limit_requires_manager_approval_before_posting(self):
        DiscountApprovalPolicy.objects.create(
            company=self.company, max_payment_amount=Decimal("50"),
            created_by=self.user, updated_by=self.user,
        )
        receipt_id, receipt_line_id = self._create_and_post_receipt()
        invoice = self.client.post("/api/purchasing/invoices/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "receipt": receipt_id,
            "invoice_number": "INV-PAY-APPROVAL", "invoice_date": "2026-03-10",
            "lines": [{"receipt_line": receipt_line_id, "unit_price": "100", "tax_rate": "14"}],
        }, format="json")
        self.assertEqual(invoice.status_code, 201, invoice.data)
        posted_invoice = self.client.post(
            f"/api/purchasing/invoices/{invoice.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(posted_invoice.status_code, 200, posted_invoice.data)
        payment = self.client.post("/api/purchasing/payments/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "invoice": invoice.data["id"],
            "payment_number": "PAY-APPROVAL", "payment_date": "2026-03-15",
            "method": "bank", "amount": "60",
        }, format="json")
        self.assertEqual(payment.status_code, 201, payment.data)
        request = FinancialApprovalRequest.objects.get(supplier_payment_id=payment.data["id"])
        blocked = self.client.post(
            f"/api/purchasing/payments/{payment.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(blocked.status_code, 400, blocked.data)
        manager = get_user_model().objects.create_user(username="payment-approval-admin", password="test-password")
        CompanyMembership.objects.create(user=manager, company=self.company, role="admin", all_branches=True)
        self.client.force_authenticate(manager)
        approved = self.client.post(
            f"/api/approvals/financial-requests/{request.pk}/approve/",
            {"note": "اعتماد سند صرف"}, format="json",
        )
        self.assertEqual(approved.status_code, 200, approved.data)
        self.client.force_authenticate(self.user)
        posted = self.client.post(
            f"/api/purchasing/payments/{payment.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(posted.status_code, 200, posted.data)

    def test_foreign_currency_nonrecoverable_tax_is_capitalized_in_base_currency_and_excluded_from_input_vat(self):
        receipt_id, receipt_line_id = self._create_and_post_receipt()
        usd = Currency.objects.create(code="USD", name_ar="دولار أمريكي")
        ExchangeRate.objects.create(
            company=self.company, currency=usd,
            rate_date=date(2026, 3, 10), rate=Decimal("50"),
        )
        code = TaxCode.objects.create(
            company=self.company, country="EG", code="FX-INPUT-NR",
            name_ar="ضريبة أجنبية غير قابلة للخصم",
            category=TaxCode.Category.STANDARD, rate=Decimal("14"),
            effective_from=date(2026, 1, 1), input_tax_recoverable=False,
        )
        created = self.client.post("/api/purchasing/invoices/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "receipt": receipt_id,
            "invoice_number": "INV-FX-NR-001", "invoice_date": "2026-03-10",
            "transaction_currency": usd.pk, "exchange_rate": "50",
            "exchange_rate_date": "2026-03-10",
            "lines": [{
                "receipt_line": receipt_line_id, "transaction_unit_price": "2",
                "tax_code": code.pk,
            }],
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        self.assertEqual(created.data["transaction_tax_total"], "0.5600")
        self.assertEqual(created.data["tax_total"], "28.0000")
        posted = self.client.post(
            f"/api/purchasing/invoices/{created.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(posted.status_code, 200, posted.data)
        self.assertEqual(posted.data["grand_total"], "228.0000")
        balance = StockBalance.objects.get(product=self.product, warehouse=self.warehouse)
        self.assertEqual(balance.inventory_value, Decimal("228.0000"))
        report = self.client.get(
            f"/api/accounting/journal-entries/vat-return/?company={self.company.pk}&date_from=2026-03-01&date_to=2026-03-31"
        )
        self.assertEqual(report.status_code, 200, report.data)
        self.assertEqual(report.data["purchases"]["tax_amount"], "28.0000")
        self.assertEqual(report.data["purchases"]["recoverable_tax_amount"], "0")
        self.assertEqual(report.data["purchases"]["nonrecoverable_tax_amount"], "28.0000")
        self.assertEqual(report.data["input_tax"], "0")
        self.assertEqual(Decimal(report.data["ledger_reconciliation"]["input_tax_difference"]), Decimal("0"))

    def test_editing_draft_foreign_purchase_invoice_recalculates_both_currency_totals(self):
        receipt_id, receipt_line_id = self._create_and_post_receipt()
        usd = Currency.objects.create(code="USD", name_ar="دولار أمريكي")
        ExchangeRate.objects.create(
            company=self.company, currency=usd,
            rate_date=date(2026, 3, 10), rate=Decimal("50"),
        )
        created = self.client.post("/api/purchasing/invoices/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "receipt": receipt_id,
            "invoice_number": "INV-FX-DRAFT-EDIT", "invoice_date": "2026-03-10",
            "transaction_currency": usd.pk, "exchange_rate": "50",
            "exchange_rate_date": "2026-03-10",
            "lines": [{
                "receipt_line": receipt_line_id, "transaction_unit_price": "2",
                "tax_rate": "14",
            }],
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        updated = self.client.patch(
            f"/api/purchasing/invoices/{created.data['id']}/",
            {"lines": [{
                "receipt_line": receipt_line_id, "transaction_unit_price": "3",
                "tax_rate": "14",
            }]},
            format="json",
        )
        self.assertEqual(updated.status_code, 200, updated.data)
        self.assertEqual(updated.data["transaction_net_total"], "6.0000")
        self.assertEqual(updated.data["transaction_tax_total"], "0.8400")
        self.assertEqual(updated.data["transaction_grand_total"], "6.8400")
        self.assertEqual(updated.data["net_total"], "300.0000")
        self.assertEqual(updated.data["tax_total"], "42.0000")
        self.assertEqual(updated.data["grand_total"], "342.0000")

    def test_foreign_supplier_invoice_partial_and_final_payments_post_realized_fx(self):
        receipt_id, receipt_line_id = self._create_and_post_receipt()
        usd = Currency.objects.create(code="USD", name_ar="دولار أمريكي")
        ExchangeRate.objects.create(company=self.company, currency=usd, rate_date=date(2026, 3, 10), rate=Decimal("50"))
        ExchangeRate.objects.create(company=self.company, currency=usd, rate_date=date(2026, 3, 11), rate=Decimal("48"))
        ExchangeRate.objects.create(company=self.company, currency=usd, rate_date=date(2026, 3, 12), rate=Decimal("52"))

        created = self.client.post("/api/purchasing/invoices/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "receipt": receipt_id,
            "invoice_number": "INV-FX-001", "invoice_date": "2026-03-10",
            "transaction_currency": usd.pk, "exchange_rate": "50", "exchange_rate_date": "2026-03-10",
            "lines": [{"receipt_line": receipt_line_id, "transaction_unit_price": "2", "tax_rate": "0"}],
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        invoice_id = created.data["id"]
        posted = self.client.post(f"/api/purchasing/invoices/{invoice_id}/post/", {}, format="json")
        self.assertEqual(posted.status_code, 200, posted.data)
        self.assertEqual(posted.data["grand_total"], "200.0000")
        self.assertEqual(posted.data["transaction_grand_total"], "4.0000")

        first = self.client.post("/api/purchasing/payments/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "invoice": invoice_id,
            "payment_number": "PAY-FX-001", "payment_date": "2026-03-11", "method": "cash",
            "amount": "48.0000", "transaction_currency": usd.pk, "transaction_amount": "1",
            "exchange_rate": "48", "exchange_rate_date": "2026-03-11",
        }, format="json")
        self.assertEqual(first.status_code, 201, first.data)
        first_posted = self.client.post(f"/api/purchasing/payments/{first.data['id']}/post/", {}, format="json")
        self.assertEqual(first_posted.status_code, 200, first_posted.data)
        self.assertEqual(first_posted.data["carrying_base_amount"], "50.0000")
        self.assertEqual(first_posted.data["realized_fx_gain"], "2.0000")
        gain_line = JournalLine.objects.get(entry_id=first_posted.data["journal_entry"], account__code="4900")
        self.assertEqual(gain_line.credit, Decimal("2.0000"))

        final = self.client.post("/api/purchasing/payments/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "invoice": invoice_id,
            "payment_number": "PAY-FX-002", "payment_date": "2026-03-12", "method": "cash",
            "amount": "156.0000", "transaction_currency": usd.pk, "transaction_amount": "3",
            "exchange_rate": "52", "exchange_rate_date": "2026-03-12",
        }, format="json")
        self.assertEqual(final.status_code, 201, final.data)
        final_posted = self.client.post(f"/api/purchasing/payments/{final.data['id']}/post/", {}, format="json")
        self.assertEqual(final_posted.status_code, 200, final_posted.data)
        self.assertEqual(final_posted.data["carrying_base_amount"], "150.0000")
        self.assertEqual(final_posted.data["realized_fx_loss"], "6.0000")
        loss_line = JournalLine.objects.get(entry_id=final_posted.data["journal_entry"], account__code="6900")
        self.assertEqual(loss_line.debit, Decimal("6.0000"))
        refreshed = self.client.get(f"/api/purchasing/invoices/{invoice_id}/").data
        self.assertEqual(refreshed["paid_total"], "200.0000")
        self.assertEqual(refreshed["balance_due"], "0.0000")

    def test_supplier_fx_revaluation_can_be_reversed_before_later_payments(self):
        receipt_id, receipt_line_id = self._create_and_post_receipt()
        usd = Currency.objects.create(code="USD", name_ar="دولار أمريكي")
        for rate_date, rate in (
            (date(2026, 3, 10), "50"), (date(2026, 3, 31), "53"),
            (date(2026, 4, 2), "51"),
        ):
            ExchangeRate.objects.create(company=self.company, currency=usd, rate_date=rate_date, rate=Decimal(rate))
        created = self.client.post("/api/purchasing/invoices/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "receipt": receipt_id,
            "invoice_number": "INV-AP-FX-REV", "invoice_date": "2026-03-10",
            "transaction_currency": usd.pk, "exchange_rate": "50", "exchange_rate_date": "2026-03-10",
            "lines": [{"receipt_line": receipt_line_id, "transaction_unit_price": "2", "tax_rate": "0"}],
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        invoice_id = created.data["id"]
        posted = self.client.post(f"/api/purchasing/invoices/{invoice_id}/post/", {}, format="json")
        self.assertEqual(posted.status_code, 200, posted.data)
        revalued = self.client.post(f"/api/purchasing/invoices/{invoice_id}/revalue/", {
            "revaluation_date": "2026-03-31", "rate_date": "2026-03-31", "rate": "53",
        }, format="json")
        self.assertEqual(revalued.status_code, 201, revalued.data)

        reversed_entry = self.client.post(
            f"/api/accounting/journal-entries/{revalued.data['journal_entry']}/reverse/",
            {"entry_date": "2026-04-01"}, format="json",
        )
        self.assertEqual(reversed_entry.status_code, 201, reversed_entry.data)
        history = self.client.get(f"/api/purchasing/invoices/{invoice_id}/revaluations/")
        self.assertEqual(history.status_code, 200, history.data)
        self.assertEqual(history.data[0]["status"], "reversed")

        later = self.client.post(f"/api/purchasing/invoices/{invoice_id}/revalue/", {
            "revaluation_date": "2026-04-02", "rate_date": "2026-04-02", "rate": "51",
        }, format="json")
        self.assertEqual(later.status_code, 201, later.data)
        self.assertEqual(later.data["previous_carrying_amount"], "200.0000")
        self.assertEqual(later.data["new_carrying_amount"], "204.0000")

        # Keep both the reversed and replacement valuations visible without
        # carrying forward the reversed unrealized amount.
        history_after = self.client.get(f"/api/purchasing/invoices/{invoice_id}/revaluations/")
        self.assertEqual(history_after.status_code, 200, history_after.data)
        self.assertEqual(len(history_after.data), 2)
        self.assertEqual(history_after.data[0]["status"], "posted")
        self.assertEqual(history_after.data[1]["status"], "reversed")
        self.assertEqual(history_after.data[1]["previous_carrying_amount"], "200.0000")
        self.assertEqual(history_after.data[1]["new_carrying_amount"], "212.0000")

        reverse_again = self.client.post(
            f"/api/accounting/journal-entries/{reversed_entry.data['id']}/reverse/",
            {"entry_date": "2026-04-03"}, format="json",
        )
        self.assertEqual(reverse_again.status_code, 400, reverse_again.data)

    def test_supplier_fx_revaluation_is_released_to_realized_fx_on_partial_and_final_payments(self):
        receipt_id, receipt_line_id = self._create_and_post_receipt()
        usd = Currency.objects.create(code="USD", name_ar="دولار أمريكي")
        for rate_date, rate in (
            (date(2026, 3, 10), "50"), (date(2026, 3, 31), "53"),
            (date(2026, 4, 1), "48"), (date(2026, 4, 2), "52"),
        ):
            ExchangeRate.objects.create(company=self.company, currency=usd, rate_date=rate_date, rate=Decimal(rate))
        created = self.client.post("/api/purchasing/invoices/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "receipt": receipt_id,
            "invoice_number": "INV-AP-FX-REVAL", "invoice_date": "2026-03-10",
            "transaction_currency": usd.pk, "exchange_rate": "50", "exchange_rate_date": "2026-03-10",
            "lines": [{"receipt_line": receipt_line_id, "transaction_unit_price": "2", "tax_rate": "0"}],
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        invoice_id = created.data["id"]
        posted = self.client.post(f"/api/purchasing/invoices/{invoice_id}/post/", {}, format="json")
        self.assertEqual(posted.status_code, 200, posted.data)

        revalued = self.client.post(f"/api/purchasing/invoices/{invoice_id}/revalue/", {
            "revaluation_date": "2026-03-31", "rate_date": "2026-03-31", "rate": "53",
        }, format="json")
        self.assertEqual(revalued.status_code, 201, revalued.data)
        self.assertEqual(revalued.data["previous_carrying_amount"], "200.0000")
        self.assertEqual(revalued.data["new_carrying_amount"], "212.0000")
        self.assertEqual(revalued.data["difference"], "12.0000")
        self.assertTrue(JournalLine.objects.filter(
            entry_id=revalued.data["journal_entry"], account__code="6160",
            debit=Decimal("12.0000"),
        ).exists())

        first = self.client.post("/api/purchasing/payments/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "invoice": invoice_id,
            "payment_number": "PAY-AP-FX-1", "payment_date": "2026-04-01", "method": "cash",
            "amount": "48.0000", "transaction_currency": usd.pk, "transaction_amount": "1",
            "exchange_rate": "48", "exchange_rate_date": "2026-04-01",
        }, format="json")
        self.assertEqual(first.status_code, 201, first.data)
        first_posted = self.client.post(f"/api/purchasing/payments/{first.data['id']}/post/", {}, format="json")
        self.assertEqual(first_posted.status_code, 200, first_posted.data)
        self.assertEqual(first_posted.data["carrying_base_amount"], "53.0000")
        self.assertEqual(first_posted.data["historical_carrying_base_amount"], "50.0000")
        self.assertEqual(first_posted.data["realized_fx_gain"], "2.0000")
        self.assertEqual(first_posted.data["realized_fx_loss"], "0.0000")

        final = self.client.post("/api/purchasing/payments/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "invoice": invoice_id,
            "payment_number": "PAY-AP-FX-2", "payment_date": "2026-04-02", "method": "cash",
            "amount": "156.0000", "transaction_currency": usd.pk, "transaction_amount": "3",
            "exchange_rate": "52", "exchange_rate_date": "2026-04-02",
        }, format="json")
        self.assertEqual(final.status_code, 201, final.data)
        final_posted = self.client.post(f"/api/purchasing/payments/{final.data['id']}/post/", {}, format="json")
        self.assertEqual(final_posted.status_code, 200, final_posted.data)
        self.assertEqual(final_posted.data["carrying_base_amount"], "159.0000")
        self.assertEqual(final_posted.data["historical_carrying_base_amount"], "150.0000")
        self.assertEqual(final_posted.data["realized_fx_gain"], "0.0000")
        self.assertEqual(final_posted.data["realized_fx_loss"], "6.0000")
        self.assertTrue(JournalLine.objects.filter(
            entry_id=final_posted.data["journal_entry"], account__code="6160",
            credit=Decimal("9.0000"),
        ).exists())

    def test_purchase_order_can_be_cancelled_before_receipt(self):
        created = self.client.post("/api/purchasing/orders/", {
            "company": self.company.pk,
            "supplier": self.supplier.pk,
            "warehouse": self.warehouse.pk,
            "order_number": "PO-CANCEL-001",
            "order_date": "2026-04-01",
            "expected_date": "2026-04-15",
            "lines": [{
                "product": self.product.pk,
                "quantity": "5",
                "unit_price": "100",
                "tax_rate": "14",
            }],
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        order_id = created.data["id"]

        confirmed = self.client.post(
            f"/api/purchasing/orders/{order_id}/confirm/", {}, format="json"
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.data)
        self.assertEqual(confirmed.data["status"], "confirmed")

        cancelled = self.client.post(
            f"/api/purchasing/orders/{order_id}/cancel/", {}, format="json"
        )
        self.assertEqual(cancelled.status_code, 200, cancelled.data)
        self.assertEqual(cancelled.data["status"], "cancelled")

        repeated = self.client.post(
            f"/api/purchasing/orders/{order_id}/cancel/", {}, format="json"
        )
        self.assertEqual(repeated.status_code, 400, repeated.data)

    def test_latest_supplier_payment_can_be_reversed_with_linked_journal(self):
        receipt_id, receipt_line_id = self._create_and_post_receipt()
        invoice = self.client.post("/api/purchasing/invoices/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "receipt": receipt_id,
            "invoice_number": "INV-REV-001", "invoice_date": "2026-03-10",
            "lines": [{"receipt_line": receipt_line_id, "unit_price": "100", "tax_rate": "14"}],
        }, format="json")
        self.assertEqual(invoice.status_code, 201, invoice.data)
        invoice_id = invoice.data["id"]
        posted_invoice = self.client.post(f"/api/purchasing/invoices/{invoice_id}/post/", {}, format="json")
        self.assertEqual(posted_invoice.status_code, 200, posted_invoice.data)
        from treasury.models import TreasuryAccount
        bank = ChartOfAccount.objects.get(company=self.company, code="1100")
        TreasuryAccount.objects.create(company=self.company, branch=self.branch, account=bank,
            kind=TreasuryAccount.Kind.BANK, code="BANK-REV", name_ar="بنك العكس", currency_code="EGP")
        payment = self.client.post("/api/purchasing/payments/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "invoice": invoice_id,
            "payment_number": "PAY-REV-001", "payment_date": "2026-03-11",
            "method": "bank", "payment_account": bank.pk, "amount": "100",
        }, format="json")
        self.assertEqual(payment.status_code, 201, payment.data)
        payment_id = payment.data["id"]
        posted_payment = self.client.post(f"/api/purchasing/payments/{payment_id}/post/", {}, format="json")
        self.assertEqual(posted_payment.status_code, 200, posted_payment.data)
        original_entry_id = posted_payment.data["journal_entry"]

        reversed_response = self.client.post(
            f"/api/purchasing/payments/{payment_id}/reverse/",
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
        invoice_after = self.client.get(f"/api/purchasing/invoices/{invoice_id}/").data
        self.assertEqual(invoice_after["paid_total"], "0")
        self.assertEqual(invoice_after["balance_due"], "228.0000")
        repeated = self.client.post(
            f"/api/purchasing/payments/{payment_id}/reverse/",
            {"entry_date": "2026-03-13"}, format="json",
        )
        self.assertEqual(repeated.status_code, 400, repeated.data)

    def test_branch_limited_user_cannot_read_or_create_documents_in_shared_default_warehouse(self):
        shared_warehouse = Warehouse.objects.create(
            company=self.company, branch=None, code="WH-SHARED", name_ar="مخزن عام"
        )
        created = self.client.post("/api/purchasing/orders/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "warehouse": shared_warehouse.pk,
            "order_number": "PO-SHARED-001", "order_date": "2026-03-10",
            "lines": [{"product": self.product.pk, "quantity": "1", "unit_price": "100", "tax_rate": "0"}],
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)

        restricted_branch = Branch.objects.create(company=self.company, code="LIMITED", name_ar="فرع محدود")
        limited_user = get_user_model().objects.create_user(
            username="purchase-branch-limited", password="test-password"
        )
        membership = CompanyMembership.objects.create(
            user=limited_user, company=self.company, role="accountant", all_branches=False
        )
        membership.branches.add(restricted_branch)
        self.client.force_authenticate(limited_user)

        listing = self.client.get(f"/api/purchasing/orders/?company={self.company.pk}")
        self.assertEqual(listing.status_code, 200, listing.data)
        self.assertNotIn(created.data["id"], [row["id"] for row in listing.data])
        detail = self.client.get(f"/api/purchasing/orders/{created.data['id']}/")
        self.assertEqual(detail.status_code, 404, detail.data)

        denied_create = self.client.post("/api/purchasing/orders/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "warehouse": shared_warehouse.pk,
            "order_number": "PO-SHARED-DENIED", "order_date": "2026-03-11",
            "lines": [{"product": self.product.pk, "quantity": "1", "unit_price": "100", "tax_rate": "0"}],
        }, format="json")
        self.assertEqual(denied_create.status_code, 403, denied_create.data)

    def test_purchase_invoice_accepts_exempt_zero_rate_code_even_when_input_is_not_recoverable(self):
        receipt_id, receipt_line_id = self._create_and_post_receipt()
        code = TaxCode.objects.create(
            company=self.company, country="EG", code="INPUT-EXEMPT", name_ar="مشتريات معفاة",
            category=TaxCode.Category.EXEMPT, rate=Decimal("0"),
            effective_from=date(2026, 1, 1), input_tax_recoverable=False,
        )
        response = self.client.post("/api/purchasing/invoices/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "receipt": receipt_id,
            "invoice_number": "INV-TAX-EXEMPT-1", "invoice_date": "2026-03-10",
            "lines": [{"receipt_line": receipt_line_id, "unit_price": "100", "tax_code": code.pk}],
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["invoice_lines"][0]["tax_code"], code.pk)
        self.assertEqual(response.data["tax_total"], "0.0000")


    def test_supplier_return_releases_only_original_receipt_cost_and_reduces_payable(self):
        receipt_id, receipt_line_id = self._create_and_post_receipt()
        invoice_response = self.client.post("/api/purchasing/invoices/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "receipt": receipt_id,
            "invoice_number": "INV-RETURN-001", "invoice_date": "2026-03-10",
            "lines": [{"receipt_line": receipt_line_id, "unit_price": "100", "tax_rate": "14"}],
        }, format="json")
        self.assertEqual(invoice_response.status_code, 201, invoice_response.data)
        invoice_id = invoice_response.data["id"]
        invoice_line_id = invoice_response.data["invoice_lines"][0]["id"]
        posted_invoice = self.client.post(
            f"/api/purchasing/invoices/{invoice_id}/post/", {}, format="json"
        )
        self.assertEqual(posted_invoice.status_code, 200, posted_invoice.data)

        created = self.client.post("/api/purchasing/returns/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "invoice": invoice_id,
            "return_number": "RET-001", "return_date": "2026-03-12",
            "lines": [{"invoice_line": invoice_line_id, "quantity": "1"}],
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        returned = self.client.post(
            f"/api/purchasing/returns/{created.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(returned.status_code, 200, returned.data)
        self.assertEqual(returned.data["status"], "posted")
        self.assertEqual(returned.data["net_amount"], "100.0000")
        self.assertEqual(returned.data["tax_amount"], "14.0000")
        self.assertEqual(returned.data["grand_total"], "114.0000")

        balance = StockBalance.objects.get(product=self.product, warehouse=self.warehouse)
        self.assertEqual(balance.quantity, Decimal("1.0000"))
        self.assertEqual(balance.inventory_value, Decimal("100.0000"))
        return_doc = SupplierReturn.objects.get(pk=returned.data["id"])
        self.assertEqual(return_doc.stock_movement.status, StockMovement.Status.POSTED)
        self.assertFalse(return_doc.stock_movement.journal_links.exists())
        self.assertEqual(
            JournalLine.objects.get(entry=return_doc.journal_entry, account__code="2200").debit,
            Decimal("114.0000"),
        )
        self.assertEqual(
            JournalLine.objects.get(entry=return_doc.journal_entry, account__code="1000").credit,
            Decimal("100.0000"),
        )
        self.assertEqual(
            JournalLine.objects.get(entry=return_doc.journal_entry, account__code="1300").credit,
            Decimal("14.0000"),
        )
        invoice_data = self.client.get(f"/api/purchasing/invoices/{invoice_id}/").data
        self.assertEqual(invoice_data["balance_due"], "114.0000")
        statement = self.client.get(
            f"/api/purchasing/invoices/supplier-statement/?company={self.company.pk}&supplier={self.supplier.pk}"
        )
        self.assertEqual(statement.status_code, 200, statement.data)
        self.assertEqual(statement.data["closing_balance"], "114.0000")
        self.assertEqual(statement.data["rows"][-1]["document_type"], "supplier_return")
