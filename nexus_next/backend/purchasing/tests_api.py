from concurrent.futures import ThreadPoolExecutor
from datetime import date
import threading
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import DatabaseError, connection, transaction
from django.db.models import Sum
from rest_framework.exceptions import ValidationError as DRFValidationError
from django.test import TransactionTestCase
from rest_framework.test import APIClient, APITestCase

from accounting.models import AccountRole, ChartOfAccount, JournalEntry, JournalLine, TaxCode
from approvals.models import DiscountApprovalPolicy, FinancialApprovalRequest
from companies.access import resolve_warehouse_branch
from companies.models import Branch, Company, CompanyActivity, CompanyMembership, Currency, ExchangeRate, FiscalYear
from inventory.models import Product, StockBalance, StockMovement, StockMovementLine, UnitOfMeasure, Warehouse
from inventory.services import post_stock_movement
from purchasing.services import supplier_credit_balance
from suppliers.models import Supplier
from treasury.models import TreasuryAccount, TreasuryTransaction
from purchasing.models import BranchPurchasePolicy, BranchPurchasePolicyAudit, LandedCostAllocation, LandedCostDocument, PurchaseInvoice, PurchaseInvoiceLine, PurchaseInvoiceTaxAllocation, PurchaseOrder, PurchaseOrderReceiptLine, PurchaseServiceAcceptance, SupplierPayment, SupplierReturn


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

        bank_account = AccountRole.objects.get(
            company=self.company, role=AccountRole.Role.BANK
        ).account
        TreasuryAccount.objects.create(
            company=self.company, branch=self.branch, account=bank_account,
            kind=TreasuryAccount.Kind.BANK, code="BANK-MAIN", name_ar="البنك الرئيسي",
            currency_code="EGP", opening_balance=Decimal("100000"),
        )
        cash_account = AccountRole.objects.get(
            company=self.company, role=AccountRole.Role.CASH
        ).account
        TreasuryAccount.objects.create(
            company=self.company, branch=self.branch, account=cash_account,
            kind=TreasuryAccount.Kind.CASH, code="CASH-MAIN", name_ar="الصندوق الرئيسي",
            currency_code="EGP", opening_balance=Decimal("100000"),
        )

    def test_unassigned_purchase_warehouse_uses_same_branch_for_validation_and_posting(self):
        from purchasing.services import _branch_and_year

        warehouse = Warehouse.objects.create(
            company=self.company, branch=None, code="WH-NO-BRANCH", name_ar="مخزن بلا فرع",
        )
        resolved = resolve_warehouse_branch(self.company, warehouse)
        posting_branch, _ = _branch_and_year(
            SimpleNamespace(company=self.company, warehouse=warehouse), date(2026, 3, 10),
        )
        self.assertEqual(resolved.pk, self.branch.pk)
        self.assertEqual(posting_branch.pk, resolved.pk)

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
        receipt_journal = JournalEntry.objects.get(entry_number=response.data["journal_number"])
        self.assertEqual(receipt_journal.source_type, JournalEntry.Source.INVENTORY)
        self.assertEqual(receipt_journal.status, JournalEntry.Status.POSTED)
        return receipt_id, receipt_line_id

    def test_draft_purchase_receipt_lines_can_be_replaced_and_order_reservation_is_recalculated(self):
        order = self.client.post("/api/purchasing/orders/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "warehouse": self.warehouse.pk,
            "order_number": "PO-RECEIPT-EDIT", "order_date": "2026-03-01",
            "lines": [{"product": self.product.pk, "quantity": "10", "unit_price": "100", "tax_rate": "14"}],
        }, format="json")
        self.assertEqual(order.status_code, 201, order.data)
        order_id = order.data["id"]
        order_line_id = order.data["line_items"][0]["id"]
        confirmed = self.client.post(f"/api/purchasing/orders/{order_id}/confirm/", {}, format="json")
        self.assertEqual(confirmed.status_code, 200, confirmed.data)

        created = self.client.post("/api/purchasing/receipts/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "warehouse": self.warehouse.pk,
            "purchase_order": order_id, "receipt_number": "GR-PO-EDIT", "receipt_date": "2026-03-05",
            "lines": [{
                "product": self.product.pk, "quantity": "4", "unit_cost": "100",
                "purchase_order_line": order_line_id,
            }],
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        receipt_id = created.data["id"]

        updated = self.client.patch(f"/api/purchasing/receipts/{receipt_id}/", {
            "receipt_date": "2026-03-06",
            "lines": [{
                "product": self.product.pk, "quantity": "6", "unit_cost": "100",
                "purchase_order_line": order_line_id,
            }],
        }, format="json")
        self.assertEqual(updated.status_code, 200, updated.data)
        self.assertEqual(updated.data["line_items"][0]["quantity"], "6.0000")
        self.assertEqual(updated.data["receipt_date"], "2026-03-06")
        self.assertEqual(
            PurchaseOrderReceiptLine.objects.filter(order_line_id=order_line_id).aggregate(
                total=Sum("quantity")
            )["total"],
            Decimal("6"),
        )
        movement = StockMovement.objects.get(movement_number="PR-GR-PO-EDIT")
        self.assertEqual(movement.movement_date, date(2026, 3, 6))
        self.assertEqual(movement.lines.count(), 1)
        self.assertEqual(movement.lines.get().quantity, Decimal("6"))

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

    def test_only_uninvoiced_draft_purchase_receipt_can_be_deleted_safely(self):
        created = self.client.post("/api/purchasing/receipts/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "warehouse": self.warehouse.pk,
            "receipt_number": "GR-DELETE-DRAFT", "receipt_date": "2026-03-10",
            "lines": [{"product": self.product.pk, "quantity": "2", "unit_cost": "100"}],
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        movement = StockMovement.objects.get(movement_number="PR-GR-DELETE-DRAFT")

        # Accountant role cannot delete purchasing documents; deletion remains admin-only.
        forbidden = self.client.delete(f"/api/purchasing/receipts/{created.data['id']}/")
        self.assertEqual(forbidden.status_code, 403, forbidden.data)

        admin = get_user_model().objects.create_user(
            username="buyer-admin-receipt-delete", password="test-password"
        )
        CompanyMembership.objects.create(
            user=admin, company=self.company, role="admin", all_branches=True,
        )
        self.client.force_authenticate(admin)
        deleted = self.client.delete(f"/api/purchasing/receipts/{created.data['id']}/")
        self.assertEqual(deleted.status_code, 204, getattr(deleted, "data", None))
        self.assertEqual(self.client.get(f"/api/purchasing/receipts/{created.data['id']}/").status_code, 404)
        self.assertFalse(StockMovement.objects.filter(pk=movement.pk).exists())
        self.assertFalse(StockBalance.objects.filter(product=self.product, warehouse=self.warehouse).exists())

        posted_id, _ = self._create_and_post_receipt()
        posted_movement = StockMovement.objects.get(movement_number="PR-GR-001")
        rejected = self.client.delete(f"/api/purchasing/receipts/{posted_id}/")
        self.assertEqual(rejected.status_code, 400, rejected.data)
        self.assertEqual(self.client.get(f"/api/purchasing/receipts/{posted_id}/").status_code, 200)
        self.assertTrue(StockMovement.objects.filter(pk=posted_movement.pk).exists())

    def test_deleting_draft_purchase_receipt_releases_purchase_order_quantity(self):
        order = self.client.post("/api/purchasing/orders/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "warehouse": self.warehouse.pk,
            "order_number": "PO-RECEIPT-DELETE", "order_date": "2026-03-01",
            "lines": [{"product": self.product.pk, "quantity": "10", "unit_price": "100", "tax_rate": "14"}],
        }, format="json")
        self.assertEqual(order.status_code, 201, order.data)
        order_id = order.data["id"]
        order_line_id = order.data["line_items"][0]["id"]
        confirmed = self.client.post(f"/api/purchasing/orders/{order_id}/confirm/", {}, format="json")
        self.assertEqual(confirmed.status_code, 200, confirmed.data)

        receipt = self.client.post("/api/purchasing/receipts/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "warehouse": self.warehouse.pk,
            "purchase_order": order_id, "receipt_number": "GR-PO-DELETE", "receipt_date": "2026-03-05",
            "lines": [{
                "product": self.product.pk, "quantity": "4", "unit_cost": "100",
                "purchase_order_line": order_line_id,
            }],
        }, format="json")
        self.assertEqual(receipt.status_code, 201, receipt.data)
        receipt_line_id = receipt.data["line_items"][0]["id"]
        self.assertTrue(PurchaseOrderReceiptLine.objects.filter(receipt_line_id=receipt_line_id).exists())
        self.assertEqual(
            PurchaseOrder.objects.get(pk=order_id).lines.get(pk=order_line_id).remaining_quantity,
            Decimal("10.0000"),
        )

        admin = get_user_model().objects.create_user(
            username="buyer-admin-po-receipt-delete", password="test-password"
        )
        CompanyMembership.objects.create(
            user=admin, company=self.company, role="admin", all_branches=True,
        )
        self.client.force_authenticate(admin)
        deleted = self.client.delete(f"/api/purchasing/receipts/{receipt.data['id']}/")
        self.assertEqual(deleted.status_code, 204, getattr(deleted, "data", None))
        self.assertFalse(PurchaseOrderReceiptLine.objects.filter(receipt_line_id=receipt_line_id).exists())
        self.assertEqual(
            PurchaseOrder.objects.get(pk=order_id).lines.get(pk=order_line_id).remaining_quantity,
            Decimal("10.0000"),
        )
        self.assertFalse(StockMovement.objects.filter(movement_number="PR-GR-PO-DELETE").exists())

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

    def test_only_untouched_draft_purchase_order_can_be_deleted(self):
        payload = {
            "company": self.company.pk, "supplier": self.supplier.pk, "warehouse": self.warehouse.pk,
            "order_number": "PO-DELETE-DRAFT", "order_date": "2026-03-10",
            "lines": [{"product": self.product.pk, "quantity": "5", "unit_price": "100", "tax_rate": "14"}],
        }
        created = self.client.post("/api/purchasing/orders/", payload, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        draft_id = created.data["id"]

        forbidden = self.client.delete(f"/api/purchasing/orders/{draft_id}/")
        self.assertEqual(forbidden.status_code, 403, forbidden.data)

        admin = get_user_model().objects.create_user(username="buyer-admin-delete", password="test-password")
        CompanyMembership.objects.create(
            user=admin, company=self.company, role="admin", all_branches=True,
        )
        self.client.force_authenticate(admin)
        deleted = self.client.delete(f"/api/purchasing/orders/{draft_id}/")
        self.assertEqual(deleted.status_code, 204, getattr(deleted, "data", None))
        self.assertEqual(self.client.get(f"/api/purchasing/orders/{draft_id}/").status_code, 404)

        payload["order_number"] = "PO-DELETE-CONFIRMED"
        confirmed_order = self.client.post("/api/purchasing/orders/", payload, format="json")
        self.assertEqual(confirmed_order.status_code, 201, confirmed_order.data)
        confirmed_id = confirmed_order.data["id"]
        confirmed = self.client.post(
            f"/api/purchasing/orders/{confirmed_id}/confirm/", {}, format="json"
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.data)

        rejected = self.client.delete(f"/api/purchasing/orders/{confirmed_id}/")
        self.assertEqual(rejected.status_code, 400, rejected.data)
        self.assertEqual(self.client.get(f"/api/purchasing/orders/{confirmed_id}/").status_code, 200)

    def test_draft_purchase_invoice_can_be_deleted_only_without_financial_dependencies(self):
        receipt_id, receipt_line_id = self._create_and_post_receipt()
        payload = {
            "company": self.company.pk, "supplier": self.supplier.pk, "receipt": receipt_id,
            "invoice_number": "INV-DRAFT-DELETE", "invoice_date": "2026-03-10",
            "lines": [{"receipt_line": receipt_line_id, "unit_price": "100", "tax_rate": "14"}],
        }
        created = self.client.post("/api/purchasing/invoices/", payload, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        draft_id = created.data["id"]

        admin = get_user_model().objects.create_user(username="buyer-admin-invoice-delete", password="test-password")
        CompanyMembership.objects.create(
            user=admin, company=self.company, role="admin", all_branches=True,
        )
        self.client.force_authenticate(admin)
        deleted = self.client.delete(f"/api/purchasing/invoices/{draft_id}/")
        self.assertEqual(deleted.status_code, 204, getattr(deleted, "data", None))
        self.assertEqual(self.client.get(f"/api/purchasing/invoices/{draft_id}/").status_code, 404)
        self.assertEqual(self.client.get(f"/api/purchasing/receipts/{receipt_id}/").status_code, 200)

        payload["invoice_number"] = "INV-POSTED-KEEP"
        posted_draft = self.client.post("/api/purchasing/invoices/", payload, format="json")
        self.assertEqual(posted_draft.status_code, 201, posted_draft.data)
        posted_id = posted_draft.data["id"]
        posted = self.client.post(
            f"/api/purchasing/invoices/{posted_id}/post/", {}, format="json"
        )
        self.assertEqual(posted.status_code, 200, posted.data)
        rejected = self.client.delete(f"/api/purchasing/invoices/{posted_id}/")
        self.assertEqual(rejected.status_code, 400, rejected.data)
        self.assertEqual(self.client.get(f"/api/purchasing/invoices/{posted_id}/").status_code, 200)

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

    def test_receipt_posting_rolls_back_inventory_and_journal_when_later_step_fails(self):
        created = self.client.post("/api/purchasing/receipts/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "warehouse": self.warehouse.pk,
            "receipt_number": "GR-ROLLBACK", "receipt_date": "2026-03-10",
            "lines": [{"product": self.product.pk, "quantity": "2", "unit_cost": "100"}],
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        receipt_id = created.data["id"]
        movement = StockMovement.objects.get(movement_number="PR-GR-ROLLBACK")

        original_post = post_stock_movement

        def post_then_fail(*args, **kwargs):
            original_post(*args, **kwargs)
            raise RuntimeError("simulated failure after inventory and journal posting")

        with patch("purchasing.services.post_stock_movement", side_effect=post_then_fail):
            with self.assertRaises(RuntimeError):
                self.client.post(
                    f"/api/purchasing/receipts/{receipt_id}/post/", {}, format="json"
                )

        movement.refresh_from_db()
        self.assertEqual(movement.status, StockMovement.Status.DRAFT)
        self.assertFalse(
            StockBalance.objects.filter(product=self.product, warehouse=self.warehouse).exists()
        )
        self.assertFalse(
            JournalEntry.objects.filter(source_type=JournalEntry.Source.INVENTORY).exists()
        )
        receipt = self.client.get(f"/api/purchasing/receipts/{receipt_id}/")
        self.assertEqual(receipt.status_code, 200, receipt.data)
        self.assertIsNone(receipt.data["journal_number"])

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

        # Trace the same receipt/invoice cycle through the financial statements.
        trial_balance = self.client.get("/api/accounting/journal-entries/trial-balance/", {
            "company": self.company.pk, "date_from": "2026-03-01", "date_to": "2026-03-31",
        })
        self.assertEqual(trial_balance.status_code, 200, trial_balance.data)
        self.assertEqual(trial_balance.data["debit_total"], Decimal("428.0000"))
        self.assertEqual(trial_balance.data["credit_total"], Decimal("428.0000"))
        self.assertEqual(trial_balance.data["difference"], Decimal("0.0000"))

        balance_sheet = self.client.get("/api/accounting/journal-entries/balance-sheet/", {
            "company": self.company.pk, "as_of": "2026-03-31",
        })
        self.assertEqual(balance_sheet.status_code, 200, balance_sheet.data)
        self.assertEqual(balance_sheet.data["difference"], Decimal("0.0000"))
        self.assertEqual(next(row["balance"] for row in balance_sheet.data["assets"] if row["code"] == "1000"), Decimal("200.0000"))
        self.assertEqual(next(row["balance"] for row in balance_sheet.data["assets"] if row["code"] == "1300"), Decimal("28.0000"))
        self.assertEqual(next(row["balance"] for row in balance_sheet.data["liabilities"] if row["code"] == "2200"), Decimal("228.0000"))

        income_statement = self.client.get("/api/accounting/journal-entries/income-statement/", {
            "company": self.company.pk, "date_from": "2026-03-01", "date_to": "2026-03-31",
        })
        self.assertEqual(income_statement.status_code, 200, income_statement.data)
        self.assertEqual(income_statement.data["cost_of_sales_total"], Decimal("0.0000"))
        self.assertEqual(income_statement.data["net_profit"], Decimal("0.0000"))

        equity = self.client.get("/api/accounting/journal-entries/changes-in-equity/", {
            "company": self.company.pk, "date_from": "2026-03-01", "date_to": "2026-03-31",
        })
        self.assertEqual(equity.status_code, 200, equity.data)
        self.assertEqual(equity.data["period_net_profit"], Decimal("0.0000"))
        self.assertEqual(equity.data["closing_equity"], Decimal("0.0000"))

        indirect_cash_flow = self.client.get("/api/accounting/journal-entries/indirect-cash-flow/", {
            "company": self.company.pk, "date_from": "2026-03-01", "date_to": "2026-03-31",
        })
        self.assertEqual(indirect_cash_flow.status_code, 200, indirect_cash_flow.data)
        self.assertEqual(indirect_cash_flow.data["operating_cash_flow"], Decimal("0.0000"))
        self.assertEqual(indirect_cash_flow.data["reconciliation_difference"], Decimal("0.0000"))

    def test_purchase_invoice_tax_rounds_to_base_currency_minor_units(self):
        receipt = self.client.post("/api/purchasing/receipts/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "warehouse": self.warehouse.pk,
            "receipt_number": "GR-TAX-ROUND", "receipt_date": "2026-03-10",
            "lines": [{"product": self.product.pk, "quantity": "2", "unit_cost": "0.3333"}],
        }, format="json")
        self.assertEqual(receipt.status_code, 201, receipt.data)
        receipt_id = receipt.data["id"]
        receipt_line_id = receipt.data["line_items"][0]["id"]
        receipt_posted = self.client.post(f"/api/purchasing/receipts/{receipt_id}/post/", {}, format="json")
        self.assertEqual(receipt_posted.status_code, 200, receipt_posted.data)
        response = self.client.post("/api/purchasing/invoices/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "receipt": receipt_id,
            "invoice_number": "INV-TAX-ROUND", "invoice_date": "2026-03-10",
            "lines": [{"receipt_line": receipt_line_id, "unit_price": "0.3333", "tax_rate": "14"}],
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        posted = self.client.post(f"/api/purchasing/invoices/{response.data['id']}/post/", {}, format="json")
        self.assertEqual(posted.status_code, 200, posted.data)
        self.assertEqual(posted.data["tax_total"], "0.0900")
        self.assertEqual(posted.data["grand_total"], "0.7566")

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

    def test_direct_service_invoice_posts_expense_input_vat_and_supplier_payable_without_stock_receipt(self):
        expense = ChartOfAccount.objects.get(company=self.company, code="6000")
        created = self.client.post("/api/purchasing/invoices/", {
            "company": self.company.pk,
            "supplier": self.supplier.pk,
            "invoice_type": "direct_expense",
            "branch": self.branch.pk,
            "invoice_number": "EXP-SVC-001",
            "invoice_date": "2026-03-15",
            "lines": [{
                "description": "صيانة أجهزة الشركة",
                "expense_account": expense.pk,
                "billed_quantity": "1",
                "unit_price": "100.0000",
                "tax_rate": "14.0000",
            }],
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        self.assertIsNone(created.data["receipt"])
        self.assertEqual(created.data["invoice_type"], "direct_expense")
        self.assertEqual(Decimal(created.data["grand_total"]), Decimal("114.0000"))

        posted = self.client.post(
            f"/api/purchasing/invoices/{created.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(posted.status_code, 200, posted.data)
        invoice = PurchaseInvoice.objects.get(pk=created.data["id"])
        self.assertEqual(invoice.status, PurchaseInvoice.Status.POSTED)
        self.assertEqual(invoice.branch_id, self.branch.pk)
        self.assertIsNone(invoice.receipt_id)

        lines = list(invoice.journal_entry.lines.select_related("account").order_by("id"))
        amounts = {
            (line.account.code, "debit" if line.debit else "credit"): line.debit or line.credit
            for line in lines
        }
        self.assertEqual(amounts[("6000", "debit")], Decimal("100.0000"))
        self.assertEqual(amounts[("1300", "debit")], Decimal("14.0000"))
        self.assertEqual(amounts[("2200", "credit")], Decimal("114.0000"))
        self.assertFalse(StockBalance.objects.filter(product=self.product, warehouse=self.warehouse).exists())

    def test_direct_service_invoice_uses_activity_specific_payable_and_vat_accounts(self):
        service = CompanyActivity.objects.create(
            company=self.company, activity=Company.Activity.SERVICE,
        )
        service_payable = ChartOfAccount.objects.create(
            company=self.company, code="2210", name_ar="موردو النشاط الخدمي",
            nature=ChartOfAccount.Nature.LIABILITY,
        )
        service_vat = ChartOfAccount.objects.create(
            company=self.company, code="1310", name_ar="ضريبة مدخلات الخدمات",
            nature=ChartOfAccount.Nature.ASSET,
        )
        AccountRole.objects.create(
            company=self.company, role=AccountRole.Role.ACCOUNTS_PAYABLE,
            account=service_payable, activity=service,
        )
        AccountRole.objects.create(
            company=self.company, role=AccountRole.Role.VAT_INPUT,
            account=service_vat, activity=service,
        )
        expense = ChartOfAccount.objects.get(company=self.company, code="6000")
        created = self.client.post("/api/purchasing/invoices/", {
            "company": self.company.pk, "supplier": self.supplier.pk,
            "activity": service.pk, "invoice_type": "direct_expense",
            "branch": self.branch.pk, "invoice_number": "EXP-SVC-ACT-001",
            "invoice_date": "2026-03-15",
            "lines": [{
                "description": "خدمة تشغيل للنشاط الخدمي",
                "expense_account": expense.pk, "billed_quantity": "1",
                "unit_price": "100", "tax_rate": "14",
            }],
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        self.assertEqual(created.data["activity"], service.pk)
        posted = self.client.post(
            f"/api/purchasing/invoices/{created.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(posted.status_code, 200, posted.data)
        posted_accounts = set(
            PurchaseInvoice.objects.get(pk=created.data["id"]).journal_entry.lines
            .values_list("account_id", flat=True)
        )
        self.assertIn(service_payable.pk, posted_accounts)
        self.assertIn(service_vat.pk, posted_accounts)
        self.assertNotIn(
            ChartOfAccount.objects.get(company=self.company, code="2200").pk,
            posted_accounts,
        )
        self.assertNotIn(
            ChartOfAccount.objects.get(company=self.company, code="1300").pk,
            posted_accounts,
        )

    def test_direct_expense_invoice_is_included_in_supplier_statement_and_aging(self):
        self.supplier.payment_terms_days = 0
        self.supplier.save(update_fields=["payment_terms_days"])
        expense = ChartOfAccount.objects.get(company=self.company, code="6000")
        created = self.client.post("/api/purchasing/invoices/", {
            "company": self.company.pk,
            "supplier": self.supplier.pk,
            "invoice_type": "direct_expense",
            "branch": self.branch.pk,
            "invoice_number": "EXP-SUBLEDGER-001",
            "invoice_date": "2026-03-10",
            "lines": [{
                "description": "اشتراك خدمات سحابية",
                "expense_account": expense.pk,
                "billed_quantity": "1",
                "unit_price": "100.0000",
                "tax_rate": "14.0000",
            }],
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        posted = self.client.post(
            "/api/purchasing/invoices/%s/post/" % created.data["id"], {}, format="json",
        )
        self.assertEqual(posted.status_code, 200, posted.data)

        statement = self.client.get(
            "/api/purchasing/invoices/supplier-statement/",
            {
                "company": self.company.pk,
                "supplier": self.supplier.pk,
                "date_from": "2026-03-01",
                "date_to": "2026-03-31",
            },
        )
        self.assertEqual(statement.status_code, 200, statement.data)
        self.assertEqual(statement.data["opening_balance"], "0")
        self.assertEqual(statement.data["closing_balance"], "114.0000")
        self.assertEqual(len(statement.data["rows"]), 1)
        self.assertEqual(statement.data["rows"][0]["document_type"], "invoice")
        self.assertEqual(statement.data["rows"][0]["document_number"], "EXP-SUBLEDGER-001")
        self.assertEqual(statement.data["rows"][0]["credit"], "114.0000")

        aging = self.client.get(
            "/api/purchasing/invoices/supplier-aging/",
            {"company": self.company.pk, "as_of": "2026-03-25"},
        )
        self.assertEqual(aging.status_code, 200, aging.data)
        self.assertEqual(aging.data["totals"]["total_due"], "114.0000")
        self.assertEqual(aging.data["suppliers"][0]["invoices"][0]["invoice_number"], "EXP-SUBLEDGER-001")
        self.assertEqual(aging.data["suppliers"][0]["invoices"][0]["balance_due"], "114.0000")

    def test_direct_service_invoice_requires_branch_and_expense_account(self):
        missing_branch = self.client.post("/api/purchasing/invoices/", {
            "company": self.company.pk, "supplier": self.supplier.pk,
            "invoice_type": "direct_expense", "invoice_number": "EXP-SVC-002",
            "invoice_date": "2026-03-15",
            "lines": [{"description": "اشتراك إنترنت", "expense_account": 1, "unit_price": "100", "tax_rate": "0"}],
        }, format="json")
        self.assertEqual(missing_branch.status_code, 400, missing_branch.data)

        missing_account = self.client.post("/api/purchasing/invoices/", {
            "company": self.company.pk, "supplier": self.supplier.pk,
            "invoice_type": "direct_expense", "branch": self.branch.pk,
            "invoice_number": "EXP-SVC-003", "invoice_date": "2026-03-15",
            "lines": [{"description": "اشتراك إنترنت", "unit_price": "100", "tax_rate": "0"}],
        }, format="json")
        self.assertEqual(missing_account.status_code, 400, missing_account.data)

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
            kind=TreasuryAccount.Kind.BANK, code="BANK-2", name_ar="البنك الفرعي", currency_code="EGP", opening_balance=Decimal("1000"))
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

    def test_end_to_end_partial_purchase_receipt_invoice_payment_return_and_refund(self):
        order = self.client.post("/api/purchasing/orders/", {
            "company": self.company.pk, "supplier": self.supplier.pk,
            "warehouse": self.warehouse.pk, "order_number": "PO-E2E-001",
            "order_date": "2026-03-01", "expected_date": "2026-03-20",
            "lines": [{
                "product": self.product.pk, "quantity": "4",
                "unit_price": "100", "tax_rate": "14",
            }],
        }, format="json")
        self.assertEqual(order.status_code, 201, order.data)
        order_id = order.data["id"]
        order_line_id = order.data["line_items"][0]["id"]
        confirmed = self.client.post(
            f"/api/purchasing/orders/{order_id}/confirm/", {}, format="json",
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.data)

        receipt = self.client.post("/api/purchasing/receipts/", {
            "company": self.company.pk, "supplier": self.supplier.pk,
            "warehouse": self.warehouse.pk, "purchase_order": order_id,
            "receipt_number": "GR-E2E-001", "receipt_date": "2026-03-05",
            "lines": [{
                "product": self.product.pk, "quantity": "2", "unit_cost": "100",
                "purchase_order_line": order_line_id,
            }],
        }, format="json")
        self.assertEqual(receipt.status_code, 201, receipt.data)
        posted_receipt = self.client.post(
            f"/api/purchasing/receipts/{receipt.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(posted_receipt.status_code, 200, posted_receipt.data)
        order_after_receipt = self.client.get(f"/api/purchasing/orders/{order_id}/").data
        self.assertEqual(order_after_receipt["status"], "partially_received")
        self.assertEqual(order_after_receipt["line_items"][0]["received_quantity"], "2.0000")
        self.assertEqual(order_after_receipt["line_items"][0]["remaining_quantity"], "2.0000")

        invoice = self.client.post("/api/purchasing/invoices/", {
            "company": self.company.pk, "supplier": self.supplier.pk,
            "receipt": receipt.data["id"], "invoice_number": "INV-E2E-001",
            "invoice_date": "2026-03-10",
            "lines": [{
                "receipt_line": receipt.data["line_items"][0]["id"],
                "unit_price": "100", "tax_rate": "14",
            }],
        }, format="json")
        self.assertEqual(invoice.status_code, 201, invoice.data)
        posted_invoice = self.client.post(
            f"/api/purchasing/invoices/{invoice.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(posted_invoice.status_code, 200, posted_invoice.data)
        self.assertEqual(posted_invoice.data["grand_total"], "228.0000")

        bank = ChartOfAccount.objects.get(company=self.company, code="1100")
        payment = self.client.post("/api/purchasing/payments/", {
            "company": self.company.pk, "supplier": self.supplier.pk,
            "invoice": invoice.data["id"], "payment_number": "PAY-E2E-001",
            "payment_date": "2026-03-11", "method": "bank",
            "payment_account": bank.pk, "amount": "200",
        }, format="json")
        self.assertEqual(payment.status_code, 201, payment.data)
        posted_payment = self.client.post(
            f"/api/purchasing/payments/{payment.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(posted_payment.status_code, 200, posted_payment.data)

        returned = self.client.post("/api/purchasing/returns/", {
            "company": self.company.pk, "supplier": self.supplier.pk,
            "invoice": invoice.data["id"], "return_number": "RET-E2E-001",
            "return_date": "2026-03-12",
            "lines": [{
                "invoice_line": invoice.data["invoice_lines"][0]["id"],
                "quantity": "1",
            }],
        }, format="json")
        self.assertEqual(returned.status_code, 201, returned.data)
        posted_return = self.client.post(
            f"/api/purchasing/returns/{returned.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(posted_return.status_code, 200, posted_return.data)
        self.assertEqual(posted_return.data["grand_total"], "114.0000")

        refund = self.client.post("/api/purchasing/refunds/", {
            "company": self.company.pk, "supplier": self.supplier.pk,
            "return_document": posted_return.data["id"],
            "refund_number": "REF-E2E-001", "refund_date": "2026-03-13",
            "method": "bank", "payment_account": bank.pk,
            "amount": "50", "reference": "BANK-E2E-REFUND",
        }, format="json")
        self.assertEqual(refund.status_code, 201, refund.data)
        posted_refund = self.client.post(
            f"/api/purchasing/refunds/{refund.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(posted_refund.status_code, 200, posted_refund.data)
        self.assertEqual(posted_refund.data["status"], "posted")

        balance = StockBalance.objects.get(product=self.product, warehouse=self.warehouse)
        self.assertEqual(balance.quantity, Decimal("1.0000"))
        self.assertEqual(balance.inventory_value, Decimal("100.0000"))
        self.assertEqual(
            JournalLine.objects.filter(account__code="2100", entry__company=self.company).aggregate(
                debit=Sum("debit"), credit=Sum("credit"),
            )["debit"],
            Decimal("200.0000"),
        )
        grni = JournalLine.objects.filter(
            account__code="2100", entry__company=self.company,
        ).aggregate(debit=Sum("debit"), credit=Sum("credit"))
        self.assertEqual(grni["debit"], grni["credit"])
        for entry in JournalEntry.objects.filter(
            company=self.company, status=JournalEntry.Status.POSTED,
        ):
            totals = entry.lines.aggregate(debit=Sum("debit"), credit=Sum("credit"))
            self.assertEqual(totals["debit"], totals["credit"], entry.entry_number)

        statement = self.client.get(
            f"/api/purchasing/invoices/supplier-statement/?company={self.company.pk}&supplier={self.supplier.pk}"
        )
        self.assertEqual(statement.status_code, 200, statement.data)
        document_types = [row["document_type"] for row in statement.data["rows"]]
        self.assertIn("supplier_return", document_types)
        self.assertIn("supplier_refund", document_types)

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

        reserved_over_receipt = self.client.post("/api/purchasing/receipts/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "warehouse": self.warehouse.pk,
            "purchase_order": order_id, "receipt_number": "GR-PO-RESERVED-OVER",
            "receipt_date": "2026-03-06",
            "lines": [{"product": self.product.pk, "quantity": "7", "unit_cost": "100",
                       "purchase_order_line": order_line_id}],
        }, format="json")
        self.assertEqual(reserved_over_receipt.status_code, 400, reserved_over_receipt.data)

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
        self.assertEqual(JournalEntry.objects.filter(source_type=JournalEntry.Source.PURCHASES).count(), 0)

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
            adjustment_reason=StockMovement.AdjustmentReason.OTHER,
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
        blocked_reversal = self.client.post(
            f"/api/accounting/journal-entries/{revalued.data['journal_entry']}/reverse/",
            {}, format="json",
        )
        self.assertEqual(blocked_reversal.status_code, 400, blocked_reversal.data)

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
        bank = ChartOfAccount.objects.get(company=self.company, code="1100")
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
        bank_mirror = TreasuryTransaction.objects.get(journal_entry_id=original_entry_id)

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
        bank_mirror.refresh_from_db()
        self.assertEqual(bank_mirror.status, TreasuryTransaction.Status.REVERSED)
        self.assertEqual(bank_mirror.reversal_journal_entry_id, reversal.pk)
        invoice_after = self.client.get(f"/api/purchasing/invoices/{invoice_id}/").data
        self.assertEqual(invoice_after["paid_total"], "0")
        self.assertEqual(invoice_after["balance_due"], "228.0000")
        repeated = self.client.post(
            f"/api/purchasing/payments/{payment_id}/reverse/",
            {"entry_date": "2026-03-13"}, format="json",
        )
        self.assertEqual(repeated.status_code, 400, repeated.data)

    def test_branch_limited_user_cannot_read_or_create_purchasing_documents_outside_assigned_branch(self):
        expense = ChartOfAccount.objects.get(company=self.company, code="6000")
        direct_invoice = self.client.post("/api/purchasing/invoices/", {
            "company": self.company.pk,
            "supplier": self.supplier.pk,
            "invoice_type": "direct_expense",
            "branch": self.branch.pk,
            "invoice_number": "EXP-BRANCH-PRIVATE",
            "invoice_date": "2026-03-10",
            "lines": [{
                "description": "مصروف فرع آخر",
                "expense_account": expense.pk,
                "billed_quantity": "1",
                "unit_price": "100",
                "tax_rate": "0",
            }],
        }, format="json")
        self.assertEqual(direct_invoice.status_code, 201, direct_invoice.data)
        posted_invoice = self.client.post(
            "/api/purchasing/invoices/%s/post/" % direct_invoice.data["id"], {}, format="json",
        )
        self.assertEqual(posted_invoice.status_code, 200, posted_invoice.data)

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

        listing = self.client.get("/api/purchasing/orders/?company=%s" % self.company.pk)
        self.assertEqual(listing.status_code, 200, listing.data)
        self.assertNotIn(created.data["id"], [row["id"] for row in listing.data])
        detail = self.client.get("/api/purchasing/orders/%s/" % created.data["id"])
        self.assertEqual(detail.status_code, 404, detail.data)

        invoice_listing = self.client.get("/api/purchasing/invoices/?company=%s" % self.company.pk)
        self.assertEqual(invoice_listing.status_code, 200, invoice_listing.data)
        self.assertNotIn(direct_invoice.data["id"], [row["id"] for row in invoice_listing.data])
        invoice_detail = self.client.get("/api/purchasing/invoices/%s/" % direct_invoice.data["id"])
        self.assertEqual(invoice_detail.status_code, 404, invoice_detail.data)
        denied_post = self.client.post(
            "/api/purchasing/invoices/%s/post/" % direct_invoice.data["id"], {}, format="json",
        )
        self.assertEqual(denied_post.status_code, 404, denied_post.data)

        restricted_statement = self.client.get(
            "/api/purchasing/invoices/supplier-statement/",
            {"company": self.company.pk, "supplier": self.supplier.pk},
        )
        self.assertEqual(restricted_statement.status_code, 200, restricted_statement.data)
        self.assertEqual(restricted_statement.data["closing_balance"], "0")
        self.assertEqual(restricted_statement.data["rows"], [])

        restricted_aging = self.client.get(
            "/api/purchasing/invoices/supplier-aging/",
            {"company": self.company.pk, "as_of": "2026-03-25"},
        )
        self.assertEqual(restricted_aging.status_code, 200, restricted_aging.data)
        self.assertEqual(restricted_aging.data["totals"]["total_due"], "0")

        denied_create = self.client.post("/api/purchasing/invoices/", {
            "company": self.company.pk,
            "supplier": self.supplier.pk,
            "invoice_type": "direct_expense",
            "branch": self.branch.pk,
            "invoice_number": "EXP-BRANCH-DENIED",
            "invoice_date": "2026-03-11",
            "lines": [{
                "description": "مصروف غير مصرح",
                "expense_account": expense.pk,
                "billed_quantity": "1",
                "unit_price": "100",
                "tax_rate": "0",
            }],
        }, format="json")
        self.assertEqual(denied_create.status_code, 403, denied_create.data)

        denied_order = self.client.post("/api/purchasing/orders/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "warehouse": shared_warehouse.pk,
            "order_number": "PO-SHARED-DENIED", "order_date": "2026-03-11",
            "lines": [{"product": self.product.pk, "quantity": "1", "unit_price": "100", "tax_rate": "0"}],
        }, format="json")
        self.assertEqual(denied_order.status_code, 403, denied_order.data)


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

        aging = self.client.get(
            f"/api/purchasing/invoices/supplier-aging/?company={self.company.pk}&as_of=2026-03-12"
        )
        self.assertEqual(aging.status_code, 200, aging.data)
        self.assertEqual(aging.data["totals"]["total_due"], "114.0000")

        vat = self.client.get(
            f"/api/accounting/journal-entries/vat-return/?company={self.company.pk}"
            "&date_from=2026-03-01&date_to=2026-03-31"
        )
        self.assertEqual(vat.status_code, 200, vat.data)
        self.assertEqual(vat.data["purchases"]["tax_amount"], "14.0000", vat.data)
        self.assertEqual(vat.data["purchases"]["recoverable_tax_amount"], "14.0000")
        self.assertEqual(vat.data["input_tax"], "14.0000")
        self.assertEqual(vat.data["ledger_reconciliation"]["input_tax_difference"], "0.0000")


    def test_supplier_refund_posts_against_return_credit_and_rejects_over_refund(self):
        receipt_id, receipt_line_id = self._create_and_post_receipt()
        invoice = self.client.post("/api/purchasing/invoices/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "receipt": receipt_id,
            "invoice_number": "INV-REFUND-001", "invoice_date": "2026-03-10",
            "lines": [{"receipt_line": receipt_line_id, "unit_price": "100", "tax_rate": "14"}],
        }, format="json")
        self.assertEqual(invoice.status_code, 201, invoice.data)
        posted_invoice = self.client.post(
            f"/api/purchasing/invoices/{invoice.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(posted_invoice.status_code, 200, posted_invoice.data)
        bank = ChartOfAccount.objects.create(
            company=self.company, code="1111", name_ar="بنك استرداد المورد", nature=ChartOfAccount.Nature.ASSET,
        )
        TreasuryAccount.objects.create(
            company=self.company, branch=self.branch, account=bank, kind=TreasuryAccount.Kind.BANK,
            code="BANK-REFUND", name_ar="بنك استرداد المورد", currency_code="EGP",
            opening_balance=Decimal("228"),
        )
        payment = self.client.post("/api/purchasing/payments/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "invoice": invoice.data["id"],
            "payment_number": "PAY-REFUND-001", "payment_date": "2026-03-11",
            "method": "bank", "payment_account": bank.pk, "amount": "228",
        }, format="json")
        self.assertEqual(payment.status_code, 201, payment.data)
        posted_payment = self.client.post(
            f"/api/purchasing/payments/{payment.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(posted_payment.status_code, 200, posted_payment.data)
        returned = self.client.post("/api/purchasing/returns/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "invoice": invoice.data["id"],
            "return_number": "RET-REFUND-001", "return_date": "2026-03-12",
            "lines": [{"invoice_line": invoice.data["invoice_lines"][0]["id"], "quantity": "1"}],
        }, format="json")
        self.assertEqual(returned.status_code, 201, returned.data)
        posted_return = self.client.post(
            f"/api/purchasing/returns/{returned.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(posted_return.status_code, 200, posted_return.data)

        refund = self.client.post("/api/purchasing/refunds/", {
            "company": self.company.pk, "supplier": self.supplier.pk,
            "return_document": posted_return.data["id"], "refund_number": "REF-001",
            "refund_date": "2026-03-13", "method": "bank", "payment_account": bank.pk,
            "amount": "50", "reference": "BANK-REF-001",
        }, format="json")
        self.assertEqual(refund.status_code, 201, refund.data)
        over_refund = self.client.post("/api/purchasing/refunds/", {
            "company": self.company.pk, "supplier": self.supplier.pk,
            "return_document": posted_return.data["id"], "refund_number": "REF-OVER",
            "refund_date": "2026-03-13", "method": "bank", "payment_account": bank.pk,
            "amount": "115",
        }, format="json")
        self.assertEqual(over_refund.status_code, 201, over_refund.data)
        rejected = self.client.post(
            f"/api/purchasing/refunds/{over_refund.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(rejected.status_code, 400, rejected.data)

        posted_refund = self.client.post(
            f"/api/purchasing/refunds/{refund.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(posted_refund.status_code, 200, posted_refund.data)
        self.assertEqual(posted_refund.data["status"], "posted")
        refund_bank_mirror = TreasuryTransaction.objects.get(
            journal_entry_id=posted_refund.data["journal_entry"],
        )
        self.assertEqual(refund_bank_mirror.status, TreasuryTransaction.Status.POSTED)
        self.assertTrue(JournalLine.objects.filter(
            entry_id=posted_refund.data["journal_entry"], account=bank, debit=Decimal("50.0000"),
        ).exists())
        self.assertTrue(JournalLine.objects.filter(
            entry_id=posted_refund.data["journal_entry"], account__code="2200", credit=Decimal("50.0000"),
        ).exists())
        statement = self.client.get(
            f"/api/purchasing/invoices/supplier-statement/?company={self.company.pk}&supplier={self.supplier.pk}"
        )
        self.assertEqual(statement.status_code, 200, statement.data)
        self.assertEqual(statement.data["rows"][-1]["document_type"], "supplier_refund")

        reversed_refund = self.client.post(
            f"/api/purchasing/refunds/{refund.data['id']}/reverse/",
            {"entry_date": "2026-03-14"}, format="json",
        )
        self.assertEqual(reversed_refund.status_code, 200, reversed_refund.data)
        self.assertEqual(reversed_refund.data["status"], "reversed")
        self.assertIsNotNone(reversed_refund.data["reversal_journal_entry"])
        self.assertEqual(
            JournalEntry.objects.get(pk=posted_refund.data["journal_entry"]).status,
            JournalEntry.Status.REVERSED,
        )
        refund_bank_mirror.refresh_from_db()
        self.assertEqual(refund_bank_mirror.status, TreasuryTransaction.Status.REVERSED)
        self.assertEqual(
            refund_bank_mirror.reversal_journal_entry_id,
            reversed_refund.data["reversal_journal_entry"],
        )
        self.assertEqual(
            supplier_credit_balance(self.company.pk, self.supplier.pk, date(2026, 3, 14)),
            Decimal("114.0000"),
        )
        statement_after_reversal = self.client.get(
            f"/api/purchasing/invoices/supplier-statement/?company={self.company.pk}&supplier={self.supplier.pk}"
        )
        self.assertEqual(statement_after_reversal.status_code, 200, statement_after_reversal.data)
        self.assertEqual(statement_after_reversal.data["rows"][-2]["document_type"], "supplier_refund")
        self.assertEqual(statement_after_reversal.data["rows"][-1]["document_type"], "supplier_refund_reversal")
        self.assertEqual(statement_after_reversal.data["closing_balance"], "-114.0000")
        repeated = self.client.post(
            f"/api/purchasing/refunds/{refund.data['id']}/reverse/",
            {"entry_date": "2026-03-15"}, format="json",
        )
        self.assertEqual(repeated.status_code, 400, repeated.data)

    def test_supplier_return_cannot_consume_another_receipts_cost_layers(self):
        first_receipt_id, first_receipt_line_id = self._create_and_post_receipt()
        second_receipt = self.client.post("/api/purchasing/receipts/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "warehouse": self.warehouse.pk,
            "receipt_number": "GR-002", "receipt_date": "2026-03-11",
            "lines": [{"product": self.product.pk, "quantity": "2", "unit_cost": "100"}],
        }, format="json")
        self.assertEqual(second_receipt.status_code, 201, second_receipt.data)
        second_post = self.client.post(
            f"/api/purchasing/receipts/{second_receipt.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(second_post.status_code, 200, second_post.data)

        issue = StockMovement.objects.create(
            company=self.company, movement_number="ISSUE-MIXED-LAYERS",
            movement_type=StockMovement.MovementType.ISSUE,
            movement_date=date(2026, 3, 12), source_warehouse=self.warehouse,
        )
        StockMovementLine.objects.create(
            movement=issue, product=self.product, quantity=Decimal("2"),
        )
        post_stock_movement(issue.pk, self.user)
        balance_before_return = StockBalance.objects.get(product=self.product, warehouse=self.warehouse)
        self.assertEqual(balance_before_return.quantity, Decimal("2.0000"))

        invoice = self.client.post("/api/purchasing/invoices/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "receipt": first_receipt_id,
            "invoice_number": "INV-RETURN-LINEAGE", "invoice_date": "2026-03-10",
            "lines": [{"receipt_line": first_receipt_line_id, "unit_price": "100", "tax_rate": "0"}],
        }, format="json")
        self.assertEqual(invoice.status_code, 201, invoice.data)
        posted_invoice = self.client.post(
            f"/api/purchasing/invoices/{invoice.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(posted_invoice.status_code, 200, posted_invoice.data)

        created = self.client.post("/api/purchasing/returns/", {
            "company": self.company.pk, "supplier": self.supplier.pk,
            "invoice": invoice.data["id"], "return_number": "RET-LINEAGE",
            "return_date": "2026-03-13",
            "lines": [{"invoice_line": invoice.data["invoice_lines"][0]["id"], "quantity": "2"}],
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        rejected = self.client.post(
            f"/api/purchasing/returns/{created.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(rejected.status_code, 400, rejected.data)
        balance_after_rejection = StockBalance.objects.get(product=self.product, warehouse=self.warehouse)
        self.assertEqual(balance_after_rejection.quantity, Decimal("2.0000"))
        self.assertEqual(
            SupplierReturn.objects.get(pk=created.data["id"]).status,
            SupplierReturn.Status.DRAFT,
        )


    def test_unpaid_foreign_supplier_return_uses_invoice_historical_rate(self):
        receipt_id, receipt_line_id = self._create_and_post_receipt()
        usd = Currency.objects.create(code="USD", name_ar="دولار أمريكي")
        ExchangeRate.objects.create(
            company=self.company, currency=usd,
            rate_date=date(2026, 3, 10), rate=Decimal("50"),
        )
        invoice = self.client.post("/api/purchasing/invoices/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "receipt": receipt_id,
            "invoice_number": "INV-FX-RETURN-001", "invoice_date": "2026-03-10",
            "transaction_currency": usd.pk, "exchange_rate": "50",
            "exchange_rate_date": "2026-03-10",
            "lines": [{"receipt_line": receipt_line_id, "transaction_unit_price": "2", "tax_rate": "14"}],
        }, format="json")
        self.assertEqual(invoice.status_code, 201, invoice.data)
        posted = self.client.post(f"/api/purchasing/invoices/{invoice.data['id']}/post/", {}, format="json")
        self.assertEqual(posted.status_code, 200, posted.data)

        draft = self.client.post("/api/purchasing/returns/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "invoice": invoice.data["id"],
            "return_number": "RET-FX-001", "return_date": "2026-03-12",
            "lines": [{"invoice_line": invoice.data["invoice_lines"][0]["id"], "quantity": "1"}],
        }, format="json")
        self.assertEqual(draft.status_code, 201, draft.data)
        returned = self.client.post(f"/api/purchasing/returns/{draft.data['id']}/post/", {}, format="json")
        self.assertEqual(returned.status_code, 200, returned.data)
        self.assertEqual(returned.data["transaction_currency_code"], "USD")
        self.assertEqual(returned.data["transaction_exchange_rate"], "50.0000000000")
        self.assertEqual(returned.data["transaction_net_amount"], "2.0000")
        self.assertEqual(returned.data["transaction_tax_amount"], "0.2800")
        self.assertEqual(returned.data["transaction_grand_total"], "2.2800")
        self.assertEqual(returned.data["grand_total"], "114.0000")
        invoice_after = self.client.get(f"/api/purchasing/invoices/{invoice.data['id']}/").data
        self.assertEqual(invoice_after["balance_due"], "114.0000")

        ExchangeRate.objects.create(
            company=self.company, currency=usd,
            rate_date=date(2026, 3, 13), rate=Decimal("48"),
        )
        payment = self.client.post("/api/purchasing/payments/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "invoice": invoice.data["id"],
            "payment_number": "PAY-FX-AFTER-RETURN", "payment_date": "2026-03-13",
            "method": "cash", "amount": "109.4400",
            "transaction_currency": usd.pk, "transaction_amount": "2.28",
            "exchange_rate": "48", "exchange_rate_date": "2026-03-13",
        }, format="json")
        self.assertEqual(payment.status_code, 201, payment.data)
        payment_posted = self.client.post(
            f"/api/purchasing/payments/{payment.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(payment_posted.status_code, 200, payment_posted.data)
        self.assertEqual(payment_posted.data["historical_carrying_base_amount"], "114.0000")
        final_invoice = self.client.get(f"/api/purchasing/invoices/{invoice.data['id']}/").data
        self.assertEqual(final_invoice["balance_due"], "0.0000")


    def test_reversing_supplier_return_restores_original_cost_layers_and_vat(self):
        receipt_id, receipt_line_id = self._create_and_post_receipt()
        invoice = self.client.post("/api/purchasing/invoices/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "receipt": receipt_id,
            "invoice_number": "INV-RETURN-REV-001", "invoice_date": "2026-03-10",
            "lines": [{"receipt_line": receipt_line_id, "unit_price": "100", "tax_rate": "14"}],
        }, format="json")
        self.assertEqual(invoice.status_code, 201, invoice.data)
        posted_invoice = self.client.post(f"/api/purchasing/invoices/{invoice.data['id']}/post/", {}, format="json")
        self.assertEqual(posted_invoice.status_code, 200, posted_invoice.data)
        draft = self.client.post("/api/purchasing/returns/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "invoice": invoice.data["id"],
            "return_number": "RET-REV-001", "return_date": "2026-03-12",
            "lines": [{"invoice_line": invoice.data["invoice_lines"][0]["id"], "quantity": "1"}],
        }, format="json")
        self.assertEqual(draft.status_code, 201, draft.data)
        posted_return = self.client.post(f"/api/purchasing/returns/{draft.data['id']}/post/", {}, format="json")
        self.assertEqual(posted_return.status_code, 200, posted_return.data)
        before_reverse = StockBalance.objects.get(product=self.product, warehouse=self.warehouse)
        self.assertEqual(before_reverse.quantity, Decimal("1.0000"))
        self.assertEqual(before_reverse.inventory_value, Decimal("100.0000"))

        reversed_return = self.client.post(
            f"/api/purchasing/returns/{draft.data['id']}/reverse/",
            {"entry_date": "2026-03-13"}, format="json",
        )
        self.assertEqual(reversed_return.status_code, 200, reversed_return.data)
        self.assertEqual(reversed_return.data["status"], "reversed")
        self.assertEqual(reversed_return.data["reversal_movement_status"], "posted")
        repeated_reverse = self.client.post(
            f"/api/purchasing/returns/{draft.data['id']}/reverse/",
            {"entry_date": "2026-03-14"}, format="json",
        )
        self.assertEqual(repeated_reverse.status_code, 400, repeated_reverse.data)
        unchanged_after_repeat = StockBalance.objects.get(product=self.product, warehouse=self.warehouse)
        self.assertEqual(unchanged_after_repeat.quantity, Decimal("2.0000"))
        self.assertEqual(unchanged_after_repeat.inventory_value, Decimal("200.0000"))
        self.assertTrue(reversed_return.data["reversal_journal_number"])

        after_reverse = StockBalance.objects.get(product=self.product, warehouse=self.warehouse)
        self.assertEqual(after_reverse.quantity, Decimal("2.0000"))
        self.assertEqual(after_reverse.inventory_value, Decimal("200.0000"))
        invoice_after = self.client.get(f"/api/purchasing/invoices/{invoice.data['id']}/").data
        self.assertEqual(invoice_after["balance_due"], "228.0000")

        return_doc = SupplierReturn.objects.get(pk=draft.data["id"])
        self.assertEqual(return_doc.status, SupplierReturn.Status.REVERSED)
        self.assertEqual(return_doc.journal_entry.status, JournalEntry.Status.REVERSED)
        self.assertEqual(return_doc.journal_entry.branch_id, return_doc.reversal_journal_entry.branch_id)
        self.assertEqual(return_doc.reversal_stock_movement.status, StockMovement.Status.POSTED)
        self.assertEqual(
            JournalLine.objects.get(entry=return_doc.reversal_journal_entry, account__code="2200").credit,
            Decimal("114.0000"),
        )
        self.assertEqual(
            JournalLine.objects.get(entry=return_doc.reversal_journal_entry, account__code="1000").debit,
            Decimal("100.0000"),
        )
        self.assertEqual(
            JournalLine.objects.get(entry=return_doc.reversal_journal_entry, account__code="1300").debit,
            Decimal("14.0000"),
        )
        statement = self.client.get(
            f"/api/purchasing/invoices/supplier-statement/?company={self.company.pk}&supplier={self.supplier.pk}"
        )
        self.assertEqual(statement.status_code, 200, statement.data)
        self.assertEqual(statement.data["closing_balance"], "228.0000")
        self.assertEqual(statement.data["rows"][-1]["document_type"], "supplier_return_reversal")
        aging_before_reverse = self.client.get(
            f"/api/purchasing/invoices/supplier-aging/?company={self.company.pk}&as_of=2026-03-12"
        )
        self.assertEqual(aging_before_reverse.status_code, 200, aging_before_reverse.data)
        self.assertEqual(aging_before_reverse.data["totals"]["total_due"], "114.0000")
        aging_after_reverse = self.client.get(
            f"/api/purchasing/invoices/supplier-aging/?company={self.company.pk}&as_of=2026-03-13"
        )
        self.assertEqual(aging_after_reverse.status_code, 200, aging_after_reverse.data)
        self.assertEqual(aging_after_reverse.data["totals"]["total_due"], "228.0000")
        vat = self.client.get(
            f"/api/accounting/journal-entries/vat-return/?company={self.company.pk}"
            "&date_from=2026-03-01&date_to=2026-03-31"
        )
        self.assertEqual(vat.status_code, 200, vat.data)
        self.assertEqual(vat.data["purchases"]["tax_amount"], "28.0000", vat.data)
        self.assertEqual(vat.data["input_tax"], "28.0000")
        self.assertEqual(Decimal(vat.data["ledger_reconciliation"]["input_tax_difference"]), Decimal("0"))

    def test_reversed_supplier_fx_revaluation_allows_foreign_currency_return(self):
        receipt_id, receipt_line_id = self._create_and_post_receipt()
        usd = Currency.objects.create(code="USD", name_ar="دولار أمريكي")
        for rate_date, rate in (
            (date(2026, 3, 10), "50"),
            (date(2026, 3, 31), "53"),
        ):
            ExchangeRate.objects.create(
                company=self.company, currency=usd,
                rate_date=rate_date, rate=Decimal(rate),
            )
        invoice = self.client.post("/api/purchasing/invoices/", {
            "company": self.company.pk, "supplier": self.supplier.pk,
            "receipt": receipt_id, "invoice_number": "INV-AP-FX-RETURN-REV",
            "invoice_date": "2026-03-10",
            "transaction_currency": usd.pk, "exchange_rate": "50",
            "exchange_rate_date": "2026-03-10",
            "lines": [{
                "receipt_line": receipt_line_id,
                "transaction_unit_price": "2", "tax_rate": "14",
            }],
        }, format="json")
        self.assertEqual(invoice.status_code, 201, invoice.data)
        posted_invoice = self.client.post(
            f"/api/purchasing/invoices/{invoice.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(posted_invoice.status_code, 200, posted_invoice.data)

        revalued = self.client.post(
            f"/api/purchasing/invoices/{invoice.data['id']}/revalue/",
            {"revaluation_date": "2026-03-31", "rate_date": "2026-03-31", "rate": "53"},
            format="json",
        )
        self.assertEqual(revalued.status_code, 201, revalued.data)
        reversed_entry = self.client.post(
            f"/api/accounting/journal-entries/{revalued.data['journal_entry']}/reverse/",
            {"entry_date": "2026-04-01"}, format="json",
        )
        self.assertEqual(reversed_entry.status_code, 201, reversed_entry.data)

        draft = self.client.post("/api/purchasing/returns/", {
            "company": self.company.pk, "supplier": self.supplier.pk,
            "invoice": invoice.data["id"], "return_number": "RET-AP-FX-REV",
            "return_date": "2026-03-12",
            "lines": [{
                "invoice_line": invoice.data["invoice_lines"][0]["id"],
                "quantity": "1",
            }],
        }, format="json")
        self.assertEqual(draft.status_code, 201, draft.data)
        returned = self.client.post(
            f"/api/purchasing/returns/{draft.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(returned.status_code, 200, returned.data)
        self.assertEqual(returned.data["status"], "posted")
        self.assertEqual(returned.data["transaction_currency_code"], "USD")
        self.assertEqual(returned.data["transaction_grand_total"], "2.2800")

        reversed_return = self.client.post(
            f"/api/purchasing/returns/{draft.data['id']}/reverse/",
            {"entry_date": "2026-04-02"}, format="json",
        )
        self.assertEqual(reversed_return.status_code, 200, reversed_return.data)
        self.assertEqual(reversed_return.data["status"], "reversed")


    def test_purchase_requisition_requires_separate_review_and_converts_once(self):
        from purchasing.models import PurchaseRequisition

        created = self.client.post("/api/purchasing/requisitions/", {
            "company": self.company.pk,
            "request_number": "REQ-001",
            "request_date": "2026-03-10",
            "required_date": "2026-03-20",
            "department": "المخازن",
            "purpose": "تغطية احتياج التشغيل",
            "lines": [{
                "product": self.product.pk,
                "requested_quantity": "5",
                "estimated_unit_price": "90",
            }],
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        requisition_id = created.data["id"]

        submitted = self.client.post(f"/api/purchasing/requisitions/{requisition_id}/submit/", {}, format="json")
        self.assertEqual(submitted.status_code, 200, submitted.data)
        self.assertEqual(submitted.data["status"], PurchaseRequisition.Status.SUBMITTED)

        self_review = self.client.post(
            f"/api/purchasing/requisitions/{requisition_id}/review/",
            {"decision": "approve"}, format="json",
        )
        self.assertEqual(self_review.status_code, 403)

        reviewer = get_user_model().objects.create_user(username="buyer-reviewer", password="test-password")
        CompanyMembership.objects.create(user=reviewer, company=self.company, role="accountant", all_branches=True)
        self.client.force_authenticate(reviewer)
        approved = self.client.post(
            f"/api/purchasing/requisitions/{requisition_id}/review/",
            {"decision": "approve", "note": "تمت مراجعة الاحتياج"}, format="json",
        )
        self.assertEqual(approved.status_code, 200, approved.data)
        self.assertEqual(approved.data["status"], PurchaseRequisition.Status.APPROVED)

        converted = self.client.post(
            f"/api/purchasing/requisitions/{requisition_id}/convert/",
            {"supplier": self.supplier.pk, "warehouse": self.warehouse.pk,
             "order_number": "PO-REQ-001", "order_date": "2026-03-11",
             "expected_date": "2026-03-20"}, format="json",
        )
        self.assertEqual(converted.status_code, 201, converted.data)
        order = PurchaseOrder.objects.get(pk=converted.data["purchase_order_id"])
        self.assertEqual(order.lines.get().quantity, Decimal("5"))
        self.assertEqual(order.lines.get().unit_price, Decimal("90"))
        requisition = PurchaseRequisition.objects.get(pk=requisition_id)
        self.assertEqual(requisition.status, PurchaseRequisition.Status.CONVERTED)
        self.assertEqual(requisition.converted_order_id, order.pk)

        duplicate_conversion = self.client.post(
            f"/api/purchasing/requisitions/{requisition_id}/convert/",
            {"supplier": self.supplier.pk, "warehouse": self.warehouse.pk,
             "order_number": "PO-REQ-002", "order_date": "2026-03-11"}, format="json",
        )
        self.assertEqual(duplicate_conversion.status_code, 400)


    def test_purchase_requisition_draft_can_be_edited(self):
        created = self.client.post("/api/purchasing/requisitions/", {
            "company": self.company.pk, "request_number": "REQ-EDIT-001",
            "request_date": "2026-03-10", "department": "المخازن",
            "lines": [{"product": self.product.pk, "requested_quantity": "2"}],
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)

        updated = self.client.patch(f"/api/purchasing/requisitions/{created.data['id']}/", {
            "request_number": "REQ-EDIT-002",
            "department": "الصيانة",
            "lines": [{"product": self.product.pk, "requested_quantity": "3",
                       "estimated_unit_price": "125"}],
        }, format="json")
        self.assertEqual(updated.status_code, 200, updated.data)
        self.assertEqual(updated.data["request_number"], "REQ-EDIT-002")
        self.assertEqual(updated.data["department"], "الصيانة")
        self.assertEqual(updated.data["line_items"][0]["requested_quantity"], "3.0000")
        self.assertEqual(updated.data["line_items"][0]["estimated_unit_price"], "125.0000")

    def test_rejected_purchase_requisition_can_be_edited_and_resubmitted(self):
        from purchasing.models import PurchaseRequisition

        created = self.client.post("/api/purchasing/requisitions/", {
            "company": self.company.pk, "request_number": "REQ-REOPEN-001",
            "request_date": "2026-03-10",
            "lines": [{"product": self.product.pk, "requested_quantity": "2",
                       "estimated_unit_price": "100"}],
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        requisition_id = created.data["id"]
        submitted = self.client.post(
            f"/api/purchasing/requisitions/{requisition_id}/submit/", {}, format="json"
        )
        self.assertEqual(submitted.status_code, 200, submitted.data)

        reviewer = get_user_model().objects.create_user(username="req-reject-reviewer", password="test-password")
        CompanyMembership.objects.create(user=reviewer, company=self.company, role="accountant", all_branches=True)
        self.client.force_authenticate(reviewer)
        rejected = self.client.post(
            f"/api/purchasing/requisitions/{requisition_id}/review/",
            {"decision": "reject", "note": "يرجى تعديل الكمية"}, format="json",
        )
        self.assertEqual(rejected.status_code, 200, rejected.data)
        self.assertEqual(rejected.data["status"], PurchaseRequisition.Status.REJECTED)

        self.client.force_authenticate(self.user)
        edited = self.client.patch(f"/api/purchasing/requisitions/{requisition_id}/", {
            "department": "التشغيل",
            "lines": [{"product": self.product.pk, "requested_quantity": "4",
                       "estimated_unit_price": "110"}],
        }, format="json")
        self.assertEqual(edited.status_code, 200, edited.data)
        self.assertEqual(edited.data["status"], PurchaseRequisition.Status.DRAFT)
        self.assertIsNone(edited.data["reviewed_by"])
        self.assertIsNone(edited.data["reviewed_at"])
        self.assertEqual(edited.data["review_note"], "")
        self.assertEqual(edited.data["line_items"][0]["requested_quantity"], "4.0000")

        resubmitted = self.client.post(
            f"/api/purchasing/requisitions/{requisition_id}/submit/", {}, format="json"
        )
        self.assertEqual(resubmitted.status_code, 200, resubmitted.data)
        self.assertEqual(resubmitted.data["status"], PurchaseRequisition.Status.SUBMITTED)

    def test_purchase_requisition_can_be_cancelled_before_approval_only(self):
        from purchasing.models import PurchaseRequisition

        draft = self.client.post("/api/purchasing/requisitions/", {
            "company": self.company.pk, "request_number": "REQ-CANCEL-DRAFT",
            "request_date": "2026-03-10",
            "lines": [{"product": self.product.pk, "requested_quantity": "1",
                       "estimated_unit_price": "100"}],
        }, format="json")
        self.assertEqual(draft.status_code, 201, draft.data)
        cancelled_draft = self.client.post(
            f"/api/purchasing/requisitions/{draft.data['id']}/cancel/", {}, format="json"
        )
        self.assertEqual(cancelled_draft.status_code, 200, cancelled_draft.data)
        self.assertEqual(cancelled_draft.data["status"], PurchaseRequisition.Status.CANCELLED)

        submitted = self.client.post("/api/purchasing/requisitions/", {
            "company": self.company.pk, "request_number": "REQ-CANCEL-SUBMITTED",
            "request_date": "2026-03-10",
            "lines": [{"product": self.product.pk, "requested_quantity": "1",
                       "estimated_unit_price": "100"}],
        }, format="json")
        self.assertEqual(submitted.status_code, 201, submitted.data)
        self.client.post(f"/api/purchasing/requisitions/{submitted.data['id']}/submit/", {}, format="json")
        cancelled_submitted = self.client.post(
            f"/api/purchasing/requisitions/{submitted.data['id']}/cancel/", {}, format="json"
        )
        self.assertEqual(cancelled_submitted.status_code, 200, cancelled_submitted.data)
        self.assertEqual(cancelled_submitted.data["status"], PurchaseRequisition.Status.CANCELLED)

        rejected = self.client.post("/api/purchasing/requisitions/", {
            "company": self.company.pk, "request_number": "REQ-CANCEL-REJECTED",
            "request_date": "2026-03-10",
            "lines": [{"product": self.product.pk, "requested_quantity": "1",
                       "estimated_unit_price": "100"}],
        }, format="json")
        self.assertEqual(rejected.status_code, 201, rejected.data)
        self.client.post(f"/api/purchasing/requisitions/{rejected.data['id']}/submit/", {}, format="json")
        reviewer = get_user_model().objects.create_user(username="req-cancel-reviewer", password="test-password")
        CompanyMembership.objects.create(user=reviewer, company=self.company, role="accountant", all_branches=True)
        self.client.force_authenticate(reviewer)
        decision = self.client.post(
            f"/api/purchasing/requisitions/{rejected.data['id']}/review/",
            {"decision": "reject", "note": "غير مكتمل"}, format="json",
        )
        self.assertEqual(decision.status_code, 200, decision.data)
        self.client.force_authenticate(self.user)
        cancelled_rejected = self.client.post(
            f"/api/purchasing/requisitions/{rejected.data['id']}/cancel/", {}, format="json"
        )
        self.assertEqual(cancelled_rejected.status_code, 200, cancelled_rejected.data)
        self.assertEqual(cancelled_rejected.data["status"], PurchaseRequisition.Status.CANCELLED)

        approved = self.client.post("/api/purchasing/requisitions/", {
            "company": self.company.pk, "request_number": "REQ-CANCEL-APPROVED",
            "request_date": "2026-03-10",
            "lines": [{"product": self.product.pk, "requested_quantity": "1",
                       "estimated_unit_price": "100"}],
        }, format="json")
        self.assertEqual(approved.status_code, 201, approved.data)
        self.client.post(f"/api/purchasing/requisitions/{approved.data['id']}/submit/", {}, format="json")
        self.client.force_authenticate(reviewer)
        approval = self.client.post(
            f"/api/purchasing/requisitions/{approved.data['id']}/review/",
            {"decision": "approve"}, format="json",
        )
        self.assertEqual(approval.status_code, 200, approval.data)
        self.client.force_authenticate(self.user)
        blocked_cancel = self.client.post(
            f"/api/purchasing/requisitions/{approved.data['id']}/cancel/", {}, format="json"
        )
        self.assertEqual(blocked_cancel.status_code, 400, blocked_cancel.data)
        self.assertEqual(
            PurchaseRequisition.objects.get(pk=approved.data["id"]).status,
            PurchaseRequisition.Status.APPROVED,
        )

    def test_requisition_conversion_requires_estimated_price_for_every_line(self):
        from purchasing.models import PurchaseRequisition

        created = self.client.post("/api/purchasing/requisitions/", {
            "company": self.company.pk, "request_number": "REQ-NO-PRICE",
            "request_date": "2026-03-10",
            "lines": [{"product": self.product.pk, "requested_quantity": "2"}],
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        requisition_id = created.data["id"]
        self.client.post(f"/api/purchasing/requisitions/{requisition_id}/submit/", {}, format="json")

        reviewer = get_user_model().objects.create_user(username="req-price-reviewer", password="test-password")
        CompanyMembership.objects.create(user=reviewer, company=self.company, role="accountant", all_branches=True)
        self.client.force_authenticate(reviewer)
        approved = self.client.post(
            f"/api/purchasing/requisitions/{requisition_id}/review/",
            {"decision": "approve"}, format="json",
        )
        self.assertEqual(approved.status_code, 200, approved.data)

        self.client.force_authenticate(self.user)
        converted = self.client.post(
            f"/api/purchasing/requisitions/{requisition_id}/convert/",
            {"supplier": self.supplier.pk, "warehouse": self.warehouse.pk,
             "order_number": "PO-REQ-NO-PRICE", "order_date": "2026-03-11"},
            format="json",
        )
        self.assertEqual(converted.status_code, 400, converted.data)
        self.assertIn("السعر التقديري", str(converted.data))
        self.assertEqual(PurchaseRequisition.objects.get(pk=requisition_id).status, PurchaseRequisition.Status.APPROVED)
        self.assertFalse(PurchaseOrder.objects.filter(order_number="PO-REQ-NO-PRICE").exists())


    def test_purchase_order_over_limit_waits_for_separate_manager_approval(self):
        DiscountApprovalPolicy.objects.create(
            company=self.company, max_purchase_order_amount=Decimal("500"),
            created_by=self.user, updated_by=self.user,
        )
        order = self.client.post("/api/purchasing/orders/", {
            "company": self.company.pk, "supplier": self.supplier.pk,
            "warehouse": self.warehouse.pk, "order_number": "PO-APPROVAL-001",
            "order_date": "2026-03-10", "expected_date": "2026-03-20",
            "lines": [{"product": self.product.pk, "quantity": "2", "unit_price": "300", "tax_rate": "0"}],
        }, format="json")
        self.assertEqual(order.status_code, 201, order.data)
        pending = self.client.post(
            f"/api/purchasing/orders/{order.data['id']}/confirm/", {}, format="json"
        )
        self.assertEqual(pending.status_code, 202, pending.data)
        request = FinancialApprovalRequest.objects.get(purchase_order_id=order.data["id"])
        self.assertEqual(request.requested_amount, Decimal("600.0000"))
        self.assertEqual(request.requested_limit, Decimal("500.0000"))
        self.assertEqual(PurchaseOrder.objects.get(pk=order.data["id"]).status, PurchaseOrder.Status.DRAFT)

        manager = get_user_model().objects.create_user(username="po-approval-admin", password="test-password")
        CompanyMembership.objects.create(user=manager, company=self.company, role="admin", all_branches=True)
        self.client.force_authenticate(manager)
        approved = self.client.post(
            f"/api/approvals/financial-requests/{request.pk}/approve/",
            {"note": "اعتماد أمر الشراء بعد مراجعة العرض"}, format="json",
        )
        self.assertEqual(approved.status_code, 200, approved.data)
        self.assertEqual(approved.data["document_type"], FinancialApprovalRequest.DocumentType.PURCHASE_ORDER)
        self.assertEqual(PurchaseOrder.objects.get(pk=order.data["id"]).status, PurchaseOrder.Status.CONFIRMED)


    def test_purchase_receipt_rejects_unit_cost_above_approved_order_price(self):
        order = self.client.post("/api/purchasing/orders/", {
            "company": self.company.pk, "supplier": self.supplier.pk,
            "warehouse": self.warehouse.pk, "order_number": "PO-PRICE-MATCH",
            "order_date": "2026-03-10", "expected_date": "2026-03-20",
            "lines": [{"product": self.product.pk, "quantity": "2", "unit_price": "100", "tax_rate": "0"}],
        }, format="json")
        self.assertEqual(order.status_code, 201, order.data)
        confirmed = self.client.post(f"/api/purchasing/orders/{order.data['id']}/confirm/", {}, format="json")
        self.assertEqual(confirmed.status_code, 200, confirmed.data)
        order_line_id = order.data["line_items"][0]["id"]
        receipt = self.client.post("/api/purchasing/receipts/", {
            "company": self.company.pk, "supplier": self.supplier.pk,
            "warehouse": self.warehouse.pk, "purchase_order": order.data["id"],
            "receipt_number": "PRICE-MISMATCH", "receipt_date": "2026-03-11",
            "lines": [{"product": self.product.pk, "quantity": "1", "unit_cost": "101",
                       "purchase_order_line": order_line_id}],
        }, format="json")
        self.assertEqual(receipt.status_code, 400, receipt.data)
        self.assertIn("سعر أمر الشراء", str(receipt.data))


    def test_grni_aging_report_includes_posted_uninvoiced_receipts(self):
        self._create_and_post_receipt()
        response = self.client.get(
            f"/api/purchasing/receipts/grni-aging/?company={self.company.pk}&as_of=2026-03-10"
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["grand_total"], "200.0000")
        self.assertEqual(response.data["aging"]["0_30"], "200.0000")
        self.assertEqual(response.data["rows"][0]["receipt_number"], "GR-001")
        self.assertEqual(response.data["monthly_summary"][0]["month"], "2026-03")
        self.assertEqual(
            response.data["gl_reconciliation"]["operational_open_receipts"], "200.0000"
        )
        grni_debug_lines = list(JournalLine.objects.filter(
            account__code="2100", entry__company=self.company,
        ).values("entry_id", "debit", "credit", "entry__source_type", "entry__description"))
        self.assertEqual(
            response.data["gl_reconciliation"]["posted_gl_balance"], "200.0000",
            grni_debug_lines,
        )
        self.assertEqual(response.data["gl_reconciliation"]["difference"], "0.0000")
        self.assertTrue(response.data["gl_reconciliation"]["reconciled"])

    def test_grni_aging_report_keeps_receipt_with_draft_supplier_invoice(self):
        receipt_id, receipt_line_id = self._create_and_post_receipt()
        invoice = self.client.post("/api/purchasing/invoices/", {
            "company": self.company.pk,
            "supplier": self.supplier.pk,
            "receipt": receipt_id,
            "invoice_number": "INV-DRAFT-GRNI",
            "invoice_date": "2026-03-10",
            "lines": [{"receipt_line": receipt_line_id, "unit_price": "100", "tax_rate": "0"}],
        }, format="json")
        self.assertEqual(invoice.status_code, 201, invoice.data)
        self.assertEqual(invoice.data["status"], "draft")

        response = self.client.get(
            f"/api/purchasing/receipts/grni-aging/?company={self.company.pk}&as_of=2026-03-10"
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["grand_total"], "200.0000")
        self.assertEqual(response.data["rows"][0]["invoice_status"], "draft")


    def test_partial_supplier_invoices_clear_grni_by_billed_quantity(self):
        receipt_id, receipt_line_id = self._create_and_post_receipt()
        first = self.client.post("/api/purchasing/invoices/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "receipt": receipt_id,
            "invoice_number": "INV-PART-001", "invoice_date": "2026-03-10",
            "lines": [{
                "receipt_line": receipt_line_id, "billed_quantity": "0.7500",
                "unit_price": "100", "tax_rate": "14",
            }],
        }, format="json")
        self.assertEqual(first.status_code, 201, first.data)
        self.assertEqual(first.data["invoice_lines"][0]["quantity"], "0.7500")
        first_posted = self.client.post(
            f"/api/purchasing/invoices/{first.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(first_posted.status_code, 200, first_posted.data)
        self.assertEqual(first_posted.data["net_total"], "75.0000")
        self.assertEqual(first_posted.data["tax_total"], "10.5000")

        after_first = self.client.get(
            f"/api/purchasing/receipts/grni-aging/?company={self.company.pk}&as_of=2026-03-10"
        )
        self.assertEqual(after_first.status_code, 200, after_first.data)
        self.assertEqual(after_first.data["grand_total"], "125.0000")
        self.assertEqual(after_first.data["rows"][0]["invoice_status"], "partially_invoiced")
        self.assertEqual(after_first.data["rows"][0]["invoiced_amount"], "75.0000")
        self.assertEqual(after_first.data["gl_reconciliation"]["posted_gl_balance"], "125.0000")
        self.assertEqual(after_first.data["gl_reconciliation"]["difference"], "0.0000")
        self.assertTrue(after_first.data["gl_reconciliation"]["reconciled"])

        second = self.client.post("/api/purchasing/invoices/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "receipt": receipt_id,
            "invoice_number": "INV-PART-002", "invoice_date": "2026-03-10",
            "lines": [{
                "receipt_line": receipt_line_id, "billed_quantity": "1.2500",
                "unit_price": "100", "tax_rate": "14",
            }],
        }, format="json")
        self.assertEqual(second.status_code, 201, second.data)
        second_posted = self.client.post(
            f"/api/purchasing/invoices/{second.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(second_posted.status_code, 200, second_posted.data)
        self.assertEqual(second_posted.data["net_total"], "125.0000")

        after_second = self.client.get(
            f"/api/purchasing/receipts/grni-aging/?company={self.company.pk}&as_of=2026-03-10"
        )
        self.assertEqual(after_second.status_code, 200, after_second.data)
        self.assertEqual(after_second.data["grand_total"], "0.0000")
        self.assertEqual(after_second.data["gl_reconciliation"]["posted_gl_balance"], "0.0000")
        self.assertEqual(after_second.data["gl_reconciliation"]["difference"], "0.0000")
        self.assertTrue(after_second.data["gl_reconciliation"]["reconciled"])

    def test_partial_supplier_invoices_cannot_overbill_a_receipt_line(self):
        receipt_id, receipt_line_id = self._create_and_post_receipt()
        first = self.client.post("/api/purchasing/invoices/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "receipt": receipt_id,
            "invoice_number": "INV-OVER-001", "invoice_date": "2026-03-10",
            "lines": [{
                "receipt_line": receipt_line_id, "billed_quantity": "1.5000",
                "unit_price": "100", "tax_rate": "0",
            }],
        }, format="json")
        self.assertEqual(first.status_code, 201, first.data)
        posted = self.client.post(
            f"/api/purchasing/invoices/{first.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(posted.status_code, 200, posted.data)

        overbilled = self.client.post("/api/purchasing/invoices/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "receipt": receipt_id,
            "invoice_number": "INV-OVER-002", "invoice_date": "2026-03-10",
            "lines": [{
                "receipt_line": receipt_line_id, "billed_quantity": "0.7500",
                "unit_price": "100", "tax_rate": "0",
            }],
        }, format="json")
        self.assertEqual(overbilled.status_code, 400, overbilled.data)
        self.assertFalse(PurchaseInvoice.objects.filter(invoice_number="INV-OVER-002").exists())
        self.assertEqual(
            PurchaseInvoiceLine.objects.filter(
                receipt_line_id=receipt_line_id, invoice__status=PurchaseInvoice.Status.POSTED,
            ).aggregate(total=Sum("billed_quantity"))["total"],
            Decimal("1.5000"),
        )


    def test_nonrecoverable_tax_waits_for_full_receipt_billing_then_capitalizes_once(self):
        receipt_id, receipt_line_id = self._create_and_post_receipt()
        tax_code = TaxCode.objects.create(
            company=self.company, country="EG", code="NR14", name_ar="ضريبة غير قابلة للخصم",
            category=TaxCode.Category.STANDARD, rate=Decimal("14"),
            effective_from=date(2026, 1, 1), input_tax_recoverable=False,
        )
        first = self.client.post("/api/purchasing/invoices/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "receipt": receipt_id,
            "invoice_number": "INV-NR-PART-001", "invoice_date": "2026-03-10",
            "lines": [{
                "receipt_line": receipt_line_id, "billed_quantity": "0.7500",
                "unit_price": "100", "tax_code": tax_code.pk,
            }],
        }, format="json")
        self.assertEqual(first.status_code, 201, first.data)
        first_posted = self.client.post(
            f"/api/purchasing/invoices/{first.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(first_posted.status_code, 200, first_posted.data)
        self.assertFalse(PurchaseInvoiceTaxAllocation.objects.filter(invoice_id=first.data["id"]).exists())
        self.assertEqual(
            StockBalance.objects.get(product=self.product, warehouse=self.warehouse).inventory_value,
            Decimal("200.0000"),
        )
        pending_return = self.client.post("/api/purchasing/returns/", {
            "company": self.company.pk, "supplier": self.supplier.pk,
            "invoice": first.data["id"], "return_number": "RET-NR-PENDING",
            "return_date": "2026-03-11",
            "lines": [{
                "invoice_line": first.data["invoice_lines"][0]["id"],
                "quantity": "0.2500",
            }],
        }, format="json")
        self.assertEqual(pending_return.status_code, 201, pending_return.data)
        blocked_return = self.client.post(
            f"/api/purchasing/returns/{pending_return.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(blocked_return.status_code, 400, blocked_return.data)

        second = self.client.post("/api/purchasing/invoices/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "receipt": receipt_id,
            "invoice_number": "INV-NR-PART-002", "invoice_date": "2026-03-10",
            "lines": [{
                "receipt_line": receipt_line_id, "billed_quantity": "1.2500",
                "unit_price": "100", "tax_code": tax_code.pk,
            }],
        }, format="json")
        self.assertEqual(second.status_code, 201, second.data)
        second_posted = self.client.post(
            f"/api/purchasing/invoices/{second.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(second_posted.status_code, 200, second_posted.data)

        balance = StockBalance.objects.get(product=self.product, warehouse=self.warehouse)
        self.assertEqual(balance.inventory_value, Decimal("228.0000"))
        allocations = PurchaseInvoiceTaxAllocation.objects.filter(invoice_id=second.data["id"])
        self.assertEqual(allocations.aggregate(total=Sum("inventory_amount"))["total"], Decimal("28.0000"))
        clearing = ChartOfAccount.objects.get(company=self.company, code="2190")
        clearing_totals = JournalLine.objects.filter(account=clearing).aggregate(
            debit=Sum("debit"), credit=Sum("credit"),
        )
        self.assertEqual(clearing_totals["debit"], Decimal("28.0000"))
        self.assertEqual(clearing_totals["credit"], Decimal("28.0000"))
        posted_return = self.client.post(
            f"/api/purchasing/returns/{pending_return.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(posted_return.status_code, 200, posted_return.data)
        self.assertEqual(posted_return.data["status"], "posted")


    def test_draft_supplier_invoice_reserves_only_its_billed_quantity(self):
        receipt_id, receipt_line_id = self._create_and_post_receipt()
        first = self.client.post("/api/purchasing/invoices/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "receipt": receipt_id,
            "invoice_number": "INV-DRAFT-RESERVE-001", "invoice_date": "2026-03-10",
            "lines": [{
                "receipt_line": receipt_line_id, "billed_quantity": "1.2500",
                "unit_price": "100", "tax_rate": "0",
            }],
        }, format="json")
        self.assertEqual(first.status_code, 201, first.data)

        remaining_invoice = self.client.post("/api/purchasing/invoices/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "receipt": receipt_id,
            "invoice_number": "INV-DRAFT-RESERVE-002", "invoice_date": "2026-03-10",
            "lines": [{
                "receipt_line": receipt_line_id, "billed_quantity": "0.7500",
                "unit_price": "100", "tax_rate": "0",
            }],
        }, format="json")
        self.assertEqual(remaining_invoice.status_code, 201, remaining_invoice.data)

        over_reserved = self.client.post("/api/purchasing/invoices/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "receipt": receipt_id,
            "invoice_number": "INV-DRAFT-RESERVE-003", "invoice_date": "2026-03-10",
            "lines": [{
                "receipt_line": receipt_line_id, "billed_quantity": "0.1000",
                "unit_price": "100", "tax_rate": "0",
            }],
        }, format="json")
        self.assertEqual(over_reserved.status_code, 400, over_reserved.data)
        self.assertFalse(PurchaseInvoice.objects.filter(invoice_number="INV-DRAFT-RESERVE-003").exists())

    def test_purchase_order_to_receipt_invoice_payment_return_and_refund_ties_together(self):
        order = self.client.post("/api/purchasing/orders/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "warehouse": self.warehouse.pk,
            "order_number": "PO-E2E-001", "order_date": "2026-03-01", "expected_date": "2026-03-20",
            "lines": [{
                "product": self.product.pk, "quantity": "2", "unit_price": "100", "tax_rate": "14",
            }],
        }, format="json")
        self.assertEqual(order.status_code, 201, order.data)
        confirmed = self.client.post(
            f"/api/purchasing/orders/{order.data['id']}/confirm/", {}, format="json",
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.data)

        receipt = self.client.post("/api/purchasing/receipts/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "warehouse": self.warehouse.pk,
            "purchase_order": order.data["id"], "receipt_number": "GR-E2E-001",
            "receipt_date": "2026-03-05",
            "lines": [{
                "product": self.product.pk, "quantity": "2", "unit_cost": "100",
                "purchase_order_line": order.data["line_items"][0]["id"],
            }],
        }, format="json")
        self.assertEqual(receipt.status_code, 201, receipt.data)
        posted_receipt = self.client.post(
            f"/api/purchasing/receipts/{receipt.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(posted_receipt.status_code, 200, posted_receipt.data)
        self.assertEqual(
            StockBalance.objects.get(product=self.product, warehouse=self.warehouse).inventory_value,
            Decimal("200.0000"),
        )

        invoice = self.client.post("/api/purchasing/invoices/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "receipt": receipt.data["id"],
            "invoice_number": "INV-E2E-001", "invoice_date": "2026-03-10",
            "lines": [{
                "receipt_line": receipt.data["line_items"][0]["id"],
                "unit_price": "100", "tax_rate": "14",
            }],
        }, format="json")
        self.assertEqual(invoice.status_code, 201, invoice.data)
        posted_invoice = self.client.post(
            f"/api/purchasing/invoices/{invoice.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(posted_invoice.status_code, 200, posted_invoice.data)
        self.assertEqual(posted_invoice.data["grand_total"], "228.0000")

        bank = ChartOfAccount.objects.create(
            company=self.company, code="1112", name_ar="بنك دورة المشتريات",
            nature=ChartOfAccount.Nature.ASSET,
        )
        TreasuryAccount.objects.create(
            company=self.company, branch=self.branch, account=bank,
            kind=TreasuryAccount.Kind.BANK, code="BANK-E2E",
            name_ar="بنك دورة المشتريات", currency_code="EGP",
            opening_balance=Decimal("500"),
        )
        payment = self.client.post("/api/purchasing/payments/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "invoice": invoice.data["id"],
            "payment_number": "PAY-E2E-001", "payment_date": "2026-03-11",
            "method": "bank", "payment_account": bank.pk, "amount": "228",
        }, format="json")
        self.assertEqual(payment.status_code, 201, payment.data)
        posted_payment = self.client.post(
            f"/api/purchasing/payments/{payment.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(posted_payment.status_code, 200, posted_payment.data)
        self.assertEqual(
            self.client.get(f"/api/purchasing/invoices/{invoice.data['id']}/").data["balance_due"],
            "0.0000",
        )

        returned = self.client.post("/api/purchasing/returns/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "invoice": invoice.data["id"],
            "return_number": "RET-E2E-001", "return_date": "2026-03-12",
            "lines": [{
                "invoice_line": invoice.data["invoice_lines"][0]["id"], "quantity": "1",
            }],
        }, format="json")
        self.assertEqual(returned.status_code, 201, returned.data)
        posted_return = self.client.post(
            f"/api/purchasing/returns/{returned.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(posted_return.status_code, 200, posted_return.data)
        self.assertEqual(posted_return.data["grand_total"], "114.0000")
        balance = StockBalance.objects.get(product=self.product, warehouse=self.warehouse)
        self.assertEqual(balance.quantity, Decimal("1.0000"))
        self.assertEqual(balance.inventory_value, Decimal("100.0000"))

        refund = self.client.post("/api/purchasing/refunds/", {
            "company": self.company.pk, "supplier": self.supplier.pk,
            "return_document": posted_return.data["id"], "refund_number": "REF-E2E-001",
            "refund_date": "2026-03-13", "method": "bank", "payment_account": bank.pk,
            "amount": "50", "reference": "BANK-E2E-REFUND",
        }, format="json")
        self.assertEqual(refund.status_code, 201, refund.data)
        posted_refund = self.client.post(
            f"/api/purchasing/refunds/{refund.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(posted_refund.status_code, 200, posted_refund.data)
        self.assertEqual(posted_refund.data["status"], "posted")
        self.assertTrue(JournalLine.objects.filter(
            entry_id=posted_refund.data["journal_entry"], account=bank,
            debit=Decimal("50.0000"),
        ).exists())
        self.assertEqual(
            supplier_credit_balance(self.company.pk, self.supplier.pk, date(2026, 3, 13)),
            Decimal("64.0000"),
        )
        statement = self.client.get(
            f"/api/purchasing/invoices/supplier-statement/?company={self.company.pk}&supplier={self.supplier.pk}"
        )
        self.assertEqual(statement.status_code, 200, statement.data)
        self.assertEqual(statement.data["rows"][-1]["document_type"], "supplier_refund")
        self.assertEqual(statement.data["closing_balance"], "-64.0000")


    def test_duplicate_supplier_invoice_number_is_rejected_without_duplicate_posting(self):
        receipt_id, receipt_line_id = self._create_and_post_receipt()
        payload = {
            "company": self.company.pk,
            "supplier": self.supplier.pk,
            "receipt": receipt_id,
            "invoice_number": "INV-DUPLICATE-CHECK",
            "invoice_date": "2026-03-10",
            "lines": [{
                "receipt_line": receipt_line_id,
                "unit_price": "100",
                "tax_rate": "14",
            }],
        }
        first = self.client.post("/api/purchasing/invoices/", payload, format="json")
        self.assertEqual(first.status_code, 201, first.data)
        posted = self.client.post(
            f"/api/purchasing/invoices/{first.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(posted.status_code, 200, posted.data)

        journal_count = JournalEntry.objects.filter(company=self.company).count()
        duplicate = self.client.post("/api/purchasing/invoices/", payload, format="json")
        self.assertEqual(duplicate.status_code, 400, duplicate.data)
        self.assertEqual(
            PurchaseInvoice.objects.filter(
                company=self.company, invoice_number="INV-DUPLICATE-CHECK",
            ).count(),
            1,
        )
        self.assertEqual(JournalEntry.objects.filter(company=self.company).count(), journal_count)
        self.assertEqual(
            JournalLine.objects.filter(account__code="2200", entry__company=self.company).aggregate(
                total=Sum("credit"),
            )["total"],
            Decimal("228.0000"),
        )


    def test_service_items_flow_through_requisition_rfq_and_supplier_quotation(self):
        from datetime import timedelta

        service = Product.objects.create(
            company=self.company,
            unit=self.unit,
            code="SVC-001",
            name_ar="صيانة دورية",
            product_type=Product.ProductType.SERVICE,
            stock_class=None,
            purchase_price=Decimal("500"),
        )
        today = date.today()

        requisition = self.client.post("/api/purchasing/requisitions/", {
            "company": self.company.pk,
            "request_number": "REQ-SVC-001",
            "request_date": today.isoformat(),
            "required_date": (today + timedelta(days=5)).isoformat(),
            "department": "الصيانة",
            "purpose": "صيانة دورية للمعدات",
            "lines": [{
                "product": service.pk,
                "requested_quantity": "1",
                "estimated_unit_price": "500",
            }],
        }, format="json")
        self.assertEqual(requisition.status_code, 201, requisition.data)
        self.assertEqual(requisition.data["line_items"][0]["product_type"], Product.ProductType.SERVICE)

        rfq = self.client.post("/api/purchasing/rfqs/", {
            "company": self.company.pk,
            "rfq_number": "RFQ-SVC-001",
            "issue_date": today.isoformat(),
            "closing_date": (today + timedelta(days=7)).isoformat(),
            "terms": "تنفيذ الخدمة بالموقع",
            "invited_suppliers": [self.supplier.pk],
            "lines": [{
                "product": service.pk,
                "quantity": "1",
                "notes": "يشمل قطع الغيار الصغيرة",
            }],
        }, format="json")
        self.assertEqual(rfq.status_code, 201, rfq.data)
        self.assertEqual(rfq.data["line_items"][0]["product_type"], Product.ProductType.SERVICE)

        opened = self.client.post(f"/api/purchasing/rfqs/{rfq.data['id']}/open/", {}, format="json")
        self.assertEqual(opened.status_code, 200, opened.data)

        quotation = self.client.post("/api/purchasing/quotations/", {
            "rfq": rfq.data["id"],
            "supplier": self.supplier.pk,
            "quotation_number": "Q-SVC-001",
            "quotation_date": today.isoformat(),
            "valid_until": (today + timedelta(days=10)).isoformat(),
            "delivery_days": 3,
            "payment_terms": "بعد اعتماد إنجاز الخدمة",
            "lines": [{
                "rfq_line": rfq.data["line_items"][0]["id"],
                "unit_price": "475",
                "discount_percent": "0",
                "tax_rate": "14",
            }],
        }, format="json")
        self.assertEqual(quotation.status_code, 201, quotation.data)
        self.assertEqual(quotation.data["line_items"][0]["product_type"], Product.ProductType.SERVICE)

        approver = get_user_model().objects.create_user(
            username="service-procurement-approver", password="test-password",
        )
        CompanyMembership.objects.create(
            user=approver, company=self.company, role="admin", all_branches=True,
        )
        self.client.force_authenticate(approver)
        awarded = self.client.post(f"/api/purchasing/rfqs/{rfq.data['id']}/award/", {
            "quotation": quotation.data["id"],
            "reason": "أفضل سعر وشروط تنفيذ مناسبة",
        }, format="json")
        self.assertEqual(awarded.status_code, 200, awarded.data)

        converted = self.client.post(f"/api/purchasing/rfqs/{rfq.data['id']}/convert-award/", {
            "warehouse": self.warehouse.pk,
            "order_number": "PO-SVC-RFQ-001",
            "order_date": today.isoformat(),
            "expected_date": (today + timedelta(days=5)).isoformat(),
        }, format="json")
        self.assertEqual(converted.status_code, 201, converted.data)
        service_order = self.client.get(f"/api/purchasing/orders/{converted.data['purchase_order_id']}/")
        self.assertEqual(service_order.status_code, 200, service_order.data)
        self.assertEqual(service_order.data["line_items"][0]["product_type"], Product.ProductType.SERVICE)


    def test_service_purchase_order_requires_service_acceptance_without_stock_receipt(self):
        service = Product.objects.create(
            company=self.company,
            unit=self.unit,
            code="SVC-002",
            name_ar="تنظيف وصيانة",
            product_type=Product.ProductType.SERVICE,
            stock_class=None,
            purchase_price=Decimal("750"),
        )
        order = self.client.post("/api/purchasing/orders/", {
            "company": self.company.pk,
            "supplier": self.supplier.pk,
            "warehouse": self.warehouse.pk,
            "order_number": "PO-SVC-002",
            "order_date": date.today().isoformat(),
            "lines": [{
                "product": service.pk,
                "quantity": "2",
                "unit_price": "750",
                "tax_rate": "14",
            }],
        }, format="json")
        self.assertEqual(order.status_code, 201, order.data)
        order_id = order.data["id"]
        order_line_id = order.data["line_items"][0]["id"]
        self.assertEqual(order.data["line_items"][0]["product_type"], Product.ProductType.SERVICE)

        confirmed = self.client.post(f"/api/purchasing/orders/{order_id}/confirm/", {}, format="json")
        self.assertEqual(confirmed.status_code, 200, confirmed.data)

        movements_before = StockMovement.objects.count()
        stock_receipt = self.client.post("/api/purchasing/receipts/", {
            "company": self.company.pk,
            "supplier": self.supplier.pk,
            "warehouse": self.warehouse.pk,
            "receipt_number": "GR-SVC-002",
            "receipt_date": date.today().isoformat(),
            "lines": [{"product": service.pk, "quantity": "1", "unit_cost": "750"}],
        }, format="json")
        self.assertEqual(stock_receipt.status_code, 400, stock_receipt.data)
        self.assertEqual(StockMovement.objects.count(), movements_before)

        acceptance = self.client.post("/api/purchasing/service-acceptances/", {
            "company": self.company.pk,
            "order": order_id,
            "supplier": self.supplier.pk,
            "acceptance_number": "SA-SVC-002",
            "acceptance_date": date.today().isoformat(),
            "notes": "تم تنفيذ الخدمة بالكامل",
            "lines": [{"order_line": order_line_id, "quantity": "2"}],
        }, format="json")
        self.assertEqual(acceptance.status_code, 201, acceptance.data)

        expense = ChartOfAccount.objects.get(company=self.company, code="6000")
        draft_linked_invoice = self.client.post("/api/purchasing/invoices/", {
            "company": self.company.pk,
            "supplier": self.supplier.pk,
            "invoice_type": "direct_expense",
            "service_acceptance": acceptance.data["id"],
            "branch": self.branch.pk,
            "invoice_number": "EXP-SVC-DRAFT-LINK",
            "invoice_date": date.today().isoformat(),
            "lines": [{
                "description": "صيانة غير معتمدة بعد", "expense_account": expense.pk,
                "billed_quantity": "1", "unit_price": "750", "tax_rate": "0",
            }],
        }, format="json")
        self.assertEqual(draft_linked_invoice.status_code, 400, draft_linked_invoice.data)

        accepted = self.client.post(
            f"/api/purchasing/service-acceptances/{acceptance.data['id']}/accept/",
            {},
            format="json",
        )
        self.assertEqual(accepted.status_code, 200, accepted.data)
        self.assertEqual(accepted.data["status"], PurchaseServiceAcceptance.Status.ACCEPTED)
        self.assertEqual(accepted.data["line_items"][0]["quantity"], "2.0000")
        self.assertEqual(StockMovement.objects.count(), movements_before)

        other_supplier = Supplier.objects.create(
            company=self.company, code="SUP-OTHER-SVC", name_ar="مورد آخر للخدمات",
        )
        mismatched_supplier_invoice = self.client.post("/api/purchasing/invoices/", {
            "company": self.company.pk,
            "supplier": other_supplier.pk,
            "invoice_type": "direct_expense",
            "service_acceptance": acceptance.data["id"],
            "branch": self.branch.pk,
            "invoice_number": "EXP-SVC-WRONG-SUPPLIER",
            "invoice_date": date.today().isoformat(),
            "lines": [{
                "description": "خدمة من مورد مختلف", "expense_account": expense.pk,
                "billed_quantity": "1", "unit_price": "750", "tax_rate": "0",
            }],
        }, format="json")
        self.assertEqual(mismatched_supplier_invoice.status_code, 400, mismatched_supplier_invoice.data)

        linked_invoice = self.client.post("/api/purchasing/invoices/", {
            "company": self.company.pk,
            "supplier": self.supplier.pk,
            "invoice_type": "direct_expense",
            "service_acceptance": acceptance.data["id"],
            "branch": self.branch.pk,
            "invoice_number": "EXP-SVC-LINKED-001",
            "invoice_date": date.today().isoformat(),
            "lines": [{
                "description": "تنفيذ خدمة الصيانة المعتمدة", "expense_account": expense.pk,
                # Same net value and tax as the accepted work, but the billed
                # quantity is wrong; value-only matching must not allow posting.
                "billed_quantity": "1", "unit_price": "1500", "tax_rate": "14",
            }],
        }, format="json")
        self.assertEqual(linked_invoice.status_code, 201, linked_invoice.data)
        self.assertEqual(linked_invoice.data["service_acceptance"], acceptance.data["id"])
        self.assertEqual(linked_invoice.data["service_acceptance_number"], "SA-SVC-002")
        overvalued_post = self.client.post(
            f"/api/purchasing/invoices/{linked_invoice.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(overvalued_post.status_code, 400, overvalued_post.data)
        self.assertIn("كمية فاتورة الخدمة", str(overvalued_post.data))
        self.assertEqual(
            PurchaseInvoice.objects.get(pk=linked_invoice.data["id"]).status,
            PurchaseInvoice.Status.DRAFT,
        )
        corrected_invoice = self.client.patch(
            f"/api/purchasing/invoices/{linked_invoice.data['id']}/",
            {"lines": [{
                "description": "تنفيذ خدمة الصيانة المعتمدة", "expense_account": expense.pk,
                "billed_quantity": "2", "unit_price": "750", "tax_rate": "14",
            }]},
            format="json",
        )
        self.assertEqual(corrected_invoice.status_code, 200, corrected_invoice.data)
        posted_linked_invoice = self.client.post(
            f"/api/purchasing/invoices/{linked_invoice.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(posted_linked_invoice.status_code, 200, posted_linked_invoice.data)
        self.assertEqual(posted_linked_invoice.data["grand_total"], "1710.0000")
        self.assertEqual(PurchaseInvoice.objects.get(pk=linked_invoice.data["id"]).service_acceptance_id, acceptance.data["id"])

        over_acceptance = self.client.post("/api/purchasing/service-acceptances/", {
            "company": self.company.pk,
            "order": order_id,
            "supplier": self.supplier.pk,
            "acceptance_number": "SA-SVC-OVER",
            "acceptance_date": date.today().isoformat(),
            "lines": [{"order_line": order_line_id, "quantity": "1"}],
        }, format="json")
        self.assertEqual(over_acceptance.status_code, 400, over_acceptance.data)


    def test_supplier_returns_accumulate_across_multiple_partial_returns(self):
        receipt_id, receipt_line_id = self._create_and_post_receipt()
        invoice = self.client.post("/api/purchasing/invoices/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "receipt": receipt_id,
            "invoice_number": "INV-MULTI-RETURN", "invoice_date": "2026-03-10",
            "lines": [{"receipt_line": receipt_line_id, "unit_price": "100", "tax_rate": "14"}],
        }, format="json")
        self.assertEqual(invoice.status_code, 201, invoice.data)
        posted_invoice = self.client.post(
            f"/api/purchasing/invoices/{invoice.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(posted_invoice.status_code, 200, posted_invoice.data)
        invoice_line_id = invoice.data["invoice_lines"][0]["id"]

        for return_number, return_date in (
            ("RET-MULTI-001", "2026-03-12"),
            ("RET-MULTI-002", "2026-03-13"),
        ):
            returned = self.client.post("/api/purchasing/returns/", {
                "company": self.company.pk, "supplier": self.supplier.pk,
                "invoice": invoice.data["id"], "return_number": return_number,
                "return_date": return_date,
                "lines": [{"invoice_line": invoice_line_id, "quantity": "1"}],
            }, format="json")
            self.assertEqual(returned.status_code, 201, returned.data)
            posted_return = self.client.post(
                f"/api/purchasing/returns/{returned.data['id']}/post/", {}, format="json",
            )
            self.assertEqual(posted_return.status_code, 200, posted_return.data)
            self.assertEqual(posted_return.data["grand_total"], "114.0000")

        excess_return = self.client.post("/api/purchasing/returns/", {
            "company": self.company.pk, "supplier": self.supplier.pk,
            "invoice": invoice.data["id"], "return_number": "RET-MULTI-003",
            "return_date": "2026-03-14",
            "lines": [{"invoice_line": invoice_line_id, "quantity": "0.01"}],
        }, format="json")
        self.assertEqual(excess_return.status_code, 400, excess_return.data)
        self.assertEqual(
            SupplierReturn.objects.filter(
                invoice_id=invoice.data["id"], status=SupplierReturn.Status.POSTED,
            ).count(),
            2,
        )


    def test_partial_payments_and_supplier_return_reconcile_supplier_statement_and_ap(self):
        receipt_id, receipt_line_id = self._create_and_post_receipt()
        invoice = self.client.post("/api/purchasing/invoices/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "receipt": receipt_id,
            "invoice_number": "INV-PAY-RETURN-001", "invoice_date": "2026-03-10",
            "lines": [{"receipt_line": receipt_line_id, "unit_price": "100", "tax_rate": "14"}],
        }, format="json")
        self.assertEqual(invoice.status_code, 201, invoice.data)
        invoice_id = invoice.data["id"]
        posted_invoice = self.client.post(
            f"/api/purchasing/invoices/{invoice_id}/post/", {}, format="json"
        )
        self.assertEqual(posted_invoice.status_code, 200, posted_invoice.data)
        self.assertEqual(posted_invoice.data["grand_total"], "228.0000")

        first_payment = self.client.post("/api/purchasing/payments/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "invoice": invoice_id,
            "payment_number": "PAY-RETURN-001", "payment_date": "2026-03-11",
            "method": "cash", "amount": "50.0000",
        }, format="json")
        self.assertEqual(first_payment.status_code, 201, first_payment.data)
        first_posted = self.client.post(
            f"/api/purchasing/payments/{first_payment.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(first_posted.status_code, 200, first_posted.data)

        returned = self.client.post("/api/purchasing/returns/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "invoice": invoice_id,
            "return_number": "RET-PAY-RETURN-001", "return_date": "2026-03-12",
            "lines": [{"invoice_line": invoice.data["invoice_lines"][0]["id"], "quantity": "1"}],
        }, format="json")
        self.assertEqual(returned.status_code, 201, returned.data)
        posted_return = self.client.post(
            f"/api/purchasing/returns/{returned.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(posted_return.status_code, 200, posted_return.data)
        self.assertEqual(posted_return.data["grand_total"], "114.0000")

        final_payment = self.client.post("/api/purchasing/payments/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "invoice": invoice_id,
            "payment_number": "PAY-RETURN-002", "payment_date": "2026-03-13",
            "method": "cash", "amount": "64.0000",
        }, format="json")
        self.assertEqual(final_payment.status_code, 201, final_payment.data)
        final_posted = self.client.post(
            f"/api/purchasing/payments/{final_payment.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(final_posted.status_code, 200, final_posted.data)

        overpayment = self.client.post("/api/purchasing/payments/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "invoice": invoice_id,
            "payment_number": "PAY-RETURN-003", "payment_date": "2026-03-14",
            "method": "cash", "amount": "0.0100",
        }, format="json")
        self.assertEqual(overpayment.status_code, 201, overpayment.data)
        rejected = self.client.post(
            f"/api/purchasing/payments/{overpayment.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(rejected.status_code, 400, rejected.data)

        invoice_data = self.client.get(f"/api/purchasing/invoices/{invoice_id}/").data
        self.assertEqual(invoice_data["paid_total"], "114.0000")
        self.assertEqual(invoice_data["balance_due"], "0.0000")
        statement = self.client.get(
            "/api/purchasing/invoices/supplier-statement/",
            {"company": self.company.pk, "supplier": self.supplier.pk,
             "date_from": "2026-03-01", "date_to": "2026-03-31"},
        )
        self.assertEqual(statement.status_code, 200, statement.data)
        self.assertEqual(statement.data["closing_balance"], "0.0000")
        ap_totals = JournalLine.objects.filter(
            account__code="2200", entry__company=self.company,
            entry__status=JournalEntry.Status.POSTED,
        ).aggregate(debit=Sum("debit"), credit=Sum("credit"))
        self.assertEqual(ap_totals["debit"], Decimal("228.0000"))
        self.assertEqual(ap_totals["credit"], Decimal("228.0000"))

    def test_grni_aging_uses_invoice_posting_date_cutoff_and_reconciles_gl(self):
        receipt_id, receipt_line_id = self._create_and_post_receipt()
        invoice = self.client.post("/api/purchasing/invoices/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "receipt": receipt_id,
            "invoice_number": "INV-GRNI-CUTOFF", "invoice_date": "2026-03-12",
            "lines": [{"receipt_line": receipt_line_id, "unit_price": "100", "tax_rate": "14"}],
        }, format="json")
        self.assertEqual(invoice.status_code, 201, invoice.data)
        posted = self.client.post(
            f"/api/purchasing/invoices/{invoice.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(posted.status_code, 200, posted.data)

        before_invoice_date = self.client.get(
            "/api/purchasing/receipts/grni-aging/",
            {"company": self.company.pk, "as_of": "2026-03-11"},
        )
        self.assertEqual(before_invoice_date.status_code, 200, before_invoice_date.data)
        self.assertEqual(before_invoice_date.data["grand_total"], "200.0000")
        self.assertEqual(
            before_invoice_date.data["gl_reconciliation"]["posted_gl_balance"], "200.0000"
        )
        self.assertEqual(before_invoice_date.data["gl_reconciliation"]["difference"], "0.0000")
        self.assertTrue(before_invoice_date.data["gl_reconciliation"]["reconciled"])

        on_invoice_date = self.client.get(
            "/api/purchasing/receipts/grni-aging/",
            {"company": self.company.pk, "as_of": "2026-03-12"},
        )
        self.assertEqual(on_invoice_date.status_code, 200, on_invoice_date.data)
        self.assertEqual(on_invoice_date.data["grand_total"], "0.0000")
        self.assertEqual(
            on_invoice_date.data["gl_reconciliation"]["posted_gl_balance"], "0.0000"
        )
        self.assertEqual(on_invoice_date.data["gl_reconciliation"]["difference"], "0.0000")
        self.assertTrue(on_invoice_date.data["gl_reconciliation"]["reconciled"])



    def test_purchase_to_sale_cycle_reconciles_grni_inventory_cogs_and_customer_balance(self):
        """Cover one product from supplier receipt through customer sale and GL posting."""
        from customers.models import Customer

        # Complete the accounting roles needed by the sales side of the same company.
        sales_roles = {
            AccountRole.Role.ACCOUNTS_RECEIVABLE: ("1200", "العملاء", ChartOfAccount.Nature.ASSET),
            AccountRole.Role.SALES: ("4000", "المبيعات", ChartOfAccount.Nature.REVENUE),
            AccountRole.Role.VAT_OUTPUT: ("2300", "ضريبة مخرجات", ChartOfAccount.Nature.LIABILITY),
        }
        for role, (code, name, nature) in sales_roles.items():
            account = ChartOfAccount.objects.create(
                company=self.company, code=code, name_ar=name, nature=nature,
            )
            AccountRole.objects.create(company=self.company, role=role, account=account)

        # Purchase: receipt creates inventory and GRNI; supplier invoice clears GRNI.
        receipt_id, receipt_line_id = self._create_and_post_receipt()
        purchase = self.client.post("/api/purchasing/invoices/", {
            "company": self.company.pk,
            "supplier": self.supplier.pk,
            "receipt": receipt_id,
            "invoice_number": "CYCLE-PUR-001",
            "invoice_date": "2026-03-11",
            "lines": [{
                "receipt_line": receipt_line_id,
                "unit_price": "100",
                "tax_rate": "14",
            }],
        }, format="json")
        self.assertEqual(purchase.status_code, 201, purchase.data)
        purchase_posted = self.client.post(
            f"/api/purchasing/invoices/{purchase.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(purchase_posted.status_code, 200, purchase_posted.data)

        # Move one unit to a second branch before sale. The transfer must preserve
        # the inventory cost and post balanced inter-branch clearing entries.
        destination_branch = Branch.objects.create(
            company=self.company, code="CYCLE-BR-02", name_ar="فرع دورة المخزون",
        )
        destination_warehouse = Warehouse.objects.create(
            company=self.company, branch=destination_branch,
            code="CYCLE-WH-02", name_ar="مخزن فرع دورة المخزون",
        )
        transfer = StockMovement.objects.create(
            company=self.company, movement_number="CYCLE-XFER-001",
            movement_type=StockMovement.MovementType.TRANSFER,
            movement_date=date(2026, 3, 11),
            source_warehouse=self.warehouse,
            destination_warehouse=destination_warehouse,
            created_by=self.user,
        )
        transfer_line = StockMovementLine.objects.create(
            movement=transfer, product=self.product, quantity=Decimal("1"),
        )
        post_stock_movement(transfer.pk, self.user)
        source_after_transfer = StockBalance.objects.get(
            product=self.product, warehouse=self.warehouse,
        )
        destination_after_transfer = StockBalance.objects.get(
            product=self.product, warehouse=destination_warehouse,
        )
        self.assertEqual(
            (source_after_transfer.quantity, source_after_transfer.inventory_value),
            (Decimal("1.0000"), Decimal("100.0000")),
        )
        self.assertEqual(
            (destination_after_transfer.quantity, destination_after_transfer.inventory_value),
            (Decimal("1.0000"), Decimal("100.0000")),
        )
        transfer_line.refresh_from_db()
        self.assertEqual(transfer_line.cost_amount, Decimal("100.0000"))
        self.assertEqual(transfer_line.unit_cost, Decimal("100.0000"))
        transfer_entries = [
            link.journal_entry for link in transfer.journal_links.select_related("journal_entry")
        ]
        self.assertEqual(len(transfer_entries), 2)
        clearing = AccountRole.objects.get(
            company=self.company, role=AccountRole.Role.INTER_BRANCH_CLEARING,
        ).account
        clearing_net = sum(
            (
                (entry.lines.filter(account=clearing).aggregate(total=Sum("debit"))["total"] or Decimal("0"))
                - (entry.lines.filter(account=clearing).aggregate(total=Sum("credit"))["total"] or Decimal("0"))
                for entry in transfer_entries
            ),
            Decimal("0"),
        )
        self.assertEqual(clearing_net, Decimal("0.0000"))

        # Sale: issue one of the two received units from the destination branch.
        customer = Customer.objects.create(
            company=self.company, code="CYCLE-CUS-001", name_ar="عميل اختبار الدورة",
        )
        delivery = self.client.post("/api/sales-documents/deliveries/", {
            "company": self.company.pk,
            "customer": customer.pk,
            "warehouse": destination_warehouse.pk,
            "delivery_number": "CYCLE-DEL-001",
            "delivery_date": "2026-03-12",
            "lines": [{"product": self.product.pk, "quantity": "1"}],
        }, format="json")
        self.assertEqual(delivery.status_code, 201, delivery.data)
        delivery_posted = self.client.post(
            f"/api/sales-documents/deliveries/{delivery.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(delivery_posted.status_code, 200, delivery_posted.data)

        sale = self.client.post("/api/sales-documents/invoices/", {
            "company": self.company.pk,
            "customer": customer.pk,
            "delivery": delivery.data["id"],
            "invoice_number": "CYCLE-SAL-001",
            "invoice_date": "2026-03-12",
            "lines": [{
                "delivery_line": delivery.data["line_items"][0]["id"],
                "unit_price": "150",
                "tax_rate": "14",
            }],
        }, format="json")
        self.assertEqual(sale.status_code, 201, sale.data)
        sale_posted = self.client.post(
            f"/api/sales-documents/invoices/{sale.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(sale_posted.status_code, 200, sale_posted.data)

        # Reconcile subledgers and inventory valuation against their posted GL accounts.
        source_balance = StockBalance.objects.get(product=self.product, warehouse=self.warehouse)
        destination_balance = StockBalance.objects.get(product=self.product, warehouse=destination_warehouse)
        self.assertEqual(
            (source_balance.quantity, source_balance.inventory_value),
            (Decimal("1.0000"), Decimal("100.0000")),
        )
        self.assertEqual(
            (destination_balance.quantity, destination_balance.inventory_value),
            (Decimal("0.0000"), Decimal("0.0000")),
        )
        self.assertEqual(
            source_balance.inventory_value + destination_balance.inventory_value,
            Decimal("100.0000"),
        )

        grni = ChartOfAccount.objects.get(company=self.company, code="2100")
        grni_totals = JournalLine.objects.filter(
            entry__company=self.company, entry__status=JournalEntry.Status.POSTED,
            account=grni,
        ).aggregate(debit=Sum("debit"), credit=Sum("credit"))
        self.assertEqual(grni_totals["debit"], Decimal("200.0000"))
        self.assertEqual(grni_totals["credit"], Decimal("200.0000"))

        inventory = ChartOfAccount.objects.get(company=self.company, code="1000")
        inventory_totals = JournalLine.objects.filter(
            entry__company=self.company, entry__status=JournalEntry.Status.POSTED,
            account=inventory,
        ).aggregate(debit=Sum("debit"), credit=Sum("credit"))
        self.assertEqual(inventory_totals["debit"] - inventory_totals["credit"], source_balance.inventory_value + destination_balance.inventory_value)

        cogs = ChartOfAccount.objects.get(company=self.company, code="5100")
        cogs_total = JournalLine.objects.filter(
            entry__company=self.company, entry__status=JournalEntry.Status.POSTED,
            account=cogs,
        ).aggregate(debit=Sum("debit"), credit=Sum("credit"))
        self.assertEqual(cogs_total["debit"] - cogs_total["credit"], Decimal("100.0000"))

        self.assertEqual(sale_posted.data["grand_total"], "171.0000")
        self.assertEqual(sale_posted.data["balance_due"], "171.0000")
        for entry in JournalEntry.objects.filter(
            company=self.company, status=JournalEntry.Status.POSTED,
        ):
            totals = entry.lines.aggregate(debit=Sum("debit"), credit=Sum("credit"))
            self.assertEqual(totals["debit"], totals["credit"], entry.entry_number)


    def test_branch_purchase_policy_blocks_new_purchase_orders_when_branch_disabled(self):
        policy = BranchPurchasePolicy.objects.create(
            branch=self.branch, purchase_enabled=False,
        )
        response = self.client.post("/api/purchasing/orders/", {
            "company": self.company.pk, "supplier": self.supplier.pk,
            "warehouse": self.warehouse.pk, "order_number": "PO-BRANCH-DISABLED",
            "order_date": "2026-03-10",
            "lines": [{"product": self.product.pk, "quantity": "1", "unit_price": "100"}],
        }, format="json")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("الشراء موقوف", str(response.data))
        self.assertFalse(PurchaseOrder.objects.filter(order_number="PO-BRANCH-DISABLED").exists())

    def test_branch_purchase_policy_restricts_suppliers_and_audits_changes(self):
        other_supplier = Supplier.objects.create(
            company=self.company, code="SUP-2", name_ar="مورد آخر",
        )
        policy_response = self.client.post("/api/purchasing/branch-purchase-policies/", {
            "branch": self.branch.pk, "purchase_enabled": True,
            "restrict_suppliers": True, "allowed_supplier_ids": [self.supplier.pk],
        }, format="json")
        self.assertEqual(policy_response.status_code, 201, policy_response.data)
        policy = BranchPurchasePolicy.objects.get(branch=self.branch)

        denied = self.client.post("/api/purchasing/orders/", {
            "company": self.company.pk, "supplier": other_supplier.pk,
            "warehouse": self.warehouse.pk, "order_number": "PO-SUPPLIER-DENIED",
            "order_date": "2026-03-10",
            "lines": [{"product": self.product.pk, "quantity": "1", "unit_price": "100"}],
        }, format="json")
        self.assertEqual(denied.status_code, 400, denied.data)
        self.assertIn("غير مسموح", str(denied.data))

        allowed = self.client.post("/api/purchasing/orders/", {
            "company": self.company.pk, "supplier": self.supplier.pk,
            "warehouse": self.warehouse.pk, "order_number": "PO-SUPPLIER-ALLOWED",
            "order_date": "2026-03-10",
            "lines": [{"product": self.product.pk, "quantity": "1", "unit_price": "100"}],
        }, format="json")
        self.assertEqual(allowed.status_code, 201, allowed.data)

        self.client.patch(
            f"/api/purchasing/branch-purchase-policies/{policy.pk}/",
            {"restrict_suppliers": False}, format="json",
        )
        self.assertEqual(BranchPurchasePolicyAudit.objects.filter(policy=policy).count(), 2)


class SupplierReturnTransactionBoundaryTests(TransactionTestCase):
    def setUp(self):
        self.client = APIClient()
        PurchasingWorkflowTests.setUp(self)

    def _create_and_post_receipt(self):
        return PurchasingWorkflowTests._create_and_post_receipt(self)

    def test_supplier_return_posting_owns_its_database_transaction(self):
        if connection.vendor != "postgresql":
            self.skipTest("This transaction-boundary regression specifically targets PostgreSQL row locking.")

        from purchasing.services import post_supplier_return

        # Outside an enclosing TestCase transaction, select_for_update must still
        # run inside the service's own atomic block. A missing ID reaches DoesNotExist;
        # without the service transaction PostgreSQL raises TransactionManagementError.
        with self.assertRaises(SupplierReturn.DoesNotExist):
            post_supplier_return(-1, user=None)




    def test_supplier_payment_rejects_amount_above_available_cash_or_bank_balance(self):
        receipt_id, receipt_line_id = self._create_and_post_receipt()
        invoice = self.client.post("/api/purchasing/invoices/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "receipt": receipt_id,
            "invoice_number": "INV-LOW-TREASURY", "invoice_date": "2026-03-10",
            "lines": [{"receipt_line": receipt_line_id, "unit_price": "100", "tax_rate": "14"}],
        }, format="json")
        self.assertEqual(invoice.status_code, 201, invoice.data)
        posted_invoice = self.client.post(
            f"/api/purchasing/invoices/{invoice.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(posted_invoice.status_code, 200, posted_invoice.data)

        for method, code, kind in (
            ("bank", "1191", TreasuryAccount.Kind.BANK),
            ("cash", "1091", TreasuryAccount.Kind.CASH),
        ):
            account = ChartOfAccount.objects.create(
                company=self.company, code=code, name_ar=f"خزينة منخفضة {method}",
                nature=ChartOfAccount.Nature.ASSET,
            )
            TreasuryAccount.objects.create(
                company=self.company, branch=self.branch, account=account, kind=kind,
                code=f"LOW-{method.upper()}", name_ar=f"خزينة منخفضة {method}",
                currency_code="EGP", opening_balance=Decimal("10"),
            )
            payment = self.client.post("/api/purchasing/payments/", {
                "company": self.company.pk, "supplier": self.supplier.pk,
                "invoice": invoice.data["id"], "payment_number": f"PAY-LOW-{method.upper()}",
                "payment_date": "2026-03-15", "method": method,
                "payment_account": account.pk, "amount": "20.0000",
            }, format="json")
            self.assertEqual(payment.status_code, 201, payment.data)
            rejected = self.client.post(
                f"/api/purchasing/payments/{payment.data['id']}/post/", {}, format="json"
            )
            self.assertEqual(rejected.status_code, 400, rejected.data)
            stored = SupplierPayment.objects.get(pk=payment.data["id"])
            self.assertEqual(stored.status, SupplierPayment.Status.DRAFT)
            self.assertIsNone(stored.journal_entry_id)
            self.assertFalse(
                JournalEntry.objects.filter(entry_number=f"PUR-PAY-{stored.pk}").exists()
            )

    def test_backdated_supplier_payment_respects_later_treasury_withdrawals(self):
        receipt_id, receipt_line_id = self._create_and_post_receipt()
        invoice = self.client.post("/api/purchasing/invoices/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "receipt": receipt_id,
            "invoice_number": "INV-BACKDATED-TREASURY", "invoice_date": "2026-03-10",
            "lines": [{"receipt_line": receipt_line_id, "unit_price": "100", "tax_rate": "14"}],
        }, format="json")
        self.assertEqual(invoice.status_code, 201, invoice.data)
        posted_invoice = self.client.post(
            f"/api/purchasing/invoices/{invoice.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(posted_invoice.status_code, 200, posted_invoice.data)

        account = ChartOfAccount.objects.create(
            company=self.company, code="1192", name_ar="بنك بحركة لاحقة",
            nature=ChartOfAccount.Nature.ASSET,
        )
        treasury_account = TreasuryAccount.objects.create(
            company=self.company, branch=self.branch, account=account,
            kind=TreasuryAccount.Kind.BANK, code="BANK-FUTURE",
            name_ar="بنك بحركة لاحقة", currency_code="EGP",
            opening_balance=Decimal("100"),
        )
        later_withdrawal = TreasuryTransaction.objects.create(
            company=self.company, branch=self.branch, fiscal_year=self.year,
            source_account=treasury_account, counter_account=ChartOfAccount.objects.get(
                company=self.company, code="6000",
            ),
            kind=TreasuryTransaction.Kind.PAYMENT, document_number="TR-FUTURE-001",
            transaction_date=date(2026, 3, 20), amount=Decimal("90"),
            description="سحب بنكي لاحق",
        )
        from treasury.services import post_treasury_transaction
        post_treasury_transaction(later_withdrawal.pk, self.user)

        payment = self.client.post("/api/purchasing/payments/", {
            "company": self.company.pk, "supplier": self.supplier.pk,
            "invoice": invoice.data["id"], "payment_number": "PAY-BACKDATED-001",
            "payment_date": "2026-03-15", "method": "bank",
            "payment_account": account.pk, "amount": "20.0000",
        }, format="json")
        self.assertEqual(payment.status_code, 201, payment.data)
        rejected = self.client.post(
            f"/api/purchasing/payments/{payment.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(rejected.status_code, 400, rejected.data)
        stored = SupplierPayment.objects.get(pk=payment.data["id"])
        self.assertEqual(stored.status, SupplierPayment.Status.DRAFT)
        self.assertIsNone(stored.journal_entry_id)

    def test_direct_service_invoice_can_be_paid_without_a_stock_receipt(self):
        expense = ChartOfAccount.objects.get(company=self.company, code="6000")
        invoice = self.client.post("/api/purchasing/invoices/", {
            "company": self.company.pk, "supplier": self.supplier.pk,
            "invoice_type": "direct_expense", "branch": self.branch.pk,
            "invoice_number": "EXP-SVC-PAY-001", "invoice_date": "2026-03-15",
            "lines": [{
                "description": "صيانة أجهزة الشركة", "expense_account": expense.pk,
                "billed_quantity": "1", "unit_price": "100", "tax_rate": "14",
            }],
        }, format="json")
        self.assertEqual(invoice.status_code, 201, invoice.data)
        posted_invoice = self.client.post(
            f"/api/purchasing/invoices/{invoice.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(posted_invoice.status_code, 200, posted_invoice.data)
        payment = self.client.post("/api/purchasing/payments/", {
            "company": self.company.pk, "supplier": self.supplier.pk,
            "invoice": invoice.data["id"], "payment_number": "PAY-SVC-001",
            "payment_date": "2026-03-16", "method": "cash", "amount": "114",
        }, format="json")
        self.assertEqual(payment.status_code, 201, payment.data)
        posted_payment = self.client.post(
            f"/api/purchasing/payments/{payment.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(posted_payment.status_code, 200, posted_payment.data)
        invoice_after = self.client.get(f"/api/purchasing/invoices/{invoice.data['id']}/").data
        self.assertEqual(invoice_after["paid_total"], "114.0000")
        self.assertEqual(invoice_after["balance_due"], "0.0000")
        self.assertEqual(
            JournalEntry.objects.get(pk=posted_payment.data["journal_entry"]).branch_id,
            self.branch.pk,
        )

    def test_purchase_order_cannot_be_cancelled_after_service_acceptance_exists(self):
        service = Product.objects.create(
            company=self.company, unit=self.unit, code="SVC-CANCEL-1",
            name_ar="خدمة لا يجوز إلغاؤها بعد قبولها", product_type=Product.ProductType.SERVICE,
            stock_class=None, purchase_price=Decimal("750"),
        )
        order = self.client.post("/api/purchasing/orders/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "warehouse": self.warehouse.pk,
            "order_number": "PO-SVC-CANCEL-1", "order_date": "2026-03-10",
            "lines": [{"product": service.pk, "quantity": "1", "unit_price": "750", "tax_rate": "0"}],
        }, format="json")
        self.assertEqual(order.status_code, 201, order.data)
        confirmed = self.client.post(f"/api/purchasing/orders/{order.data['id']}/confirm/", {}, format="json")
        self.assertEqual(confirmed.status_code, 200, confirmed.data)
        acceptance = self.client.post("/api/purchasing/service-acceptances/", {
            "company": self.company.pk, "order": order.data["id"], "supplier": self.supplier.pk,
            "acceptance_number": "SA-SVC-CANCEL-1", "acceptance_date": "2026-03-11",
            "lines": [{"order_line": order.data["line_items"][0]["id"], "quantity": "1"}],
        }, format="json")
        self.assertEqual(acceptance.status_code, 201, acceptance.data)
        cancelled = self.client.post(f"/api/purchasing/orders/{order.data['id']}/cancel/", {}, format="json")
        self.assertEqual(cancelled.status_code, 400, cancelled.data)
        self.assertEqual(
            PurchaseOrder.objects.get(pk=order.data["id"]).status,
            PurchaseOrder.Status.CONFIRMED,
        )

    def test_service_invoice_must_use_the_acceptance_order_branch(self):
        service = Product.objects.create(
            company=self.company, unit=self.unit, code="SVC-BRANCH-1",
            name_ar="خدمة مرتبطة بفرع", product_type=Product.ProductType.SERVICE,
            stock_class=None, purchase_price=Decimal("750"),
        )
        order = self.client.post("/api/purchasing/orders/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "warehouse": self.warehouse.pk,
            "order_number": "PO-SVC-BRANCH-1", "order_date": "2026-03-10",
            "lines": [{"product": service.pk, "quantity": "1", "unit_price": "750", "tax_rate": "0"}],
        }, format="json")
        self.assertEqual(order.status_code, 201, order.data)
        confirmed = self.client.post(f"/api/purchasing/orders/{order.data['id']}/confirm/", {}, format="json")
        self.assertEqual(confirmed.status_code, 200, confirmed.data)
        acceptance = self.client.post("/api/purchasing/service-acceptances/", {
            "company": self.company.pk, "order": order.data["id"], "supplier": self.supplier.pk,
            "acceptance_number": "SA-SVC-BRANCH-1", "acceptance_date": "2026-03-11",
            "lines": [{"order_line": order.data["line_items"][0]["id"], "quantity": "1"}],
        }, format="json")
        self.assertEqual(acceptance.status_code, 201, acceptance.data)
        self.assertEqual(acceptance.data["branch"], self.branch.pk)
        other_branch = Branch.objects.create(
            company=self.company, code="BR-SVC-OTHER", name_ar="فرع مختلف",
        )
        expense = ChartOfAccount.objects.get(company=self.company, code="6000")
        invoice = self.client.post("/api/purchasing/invoices/", {
            "company": self.company.pk, "supplier": self.supplier.pk,
            "invoice_type": "direct_expense", "service_acceptance": acceptance.data["id"],
            "branch": other_branch.pk, "invoice_number": "EXP-SVC-WRONG-BRANCH",
            "invoice_date": "2026-03-12",
            "lines": [{
                "description": "خدمة في فرع مختلف", "expense_account": expense.pk,
                "billed_quantity": "1", "unit_price": "750", "tax_rate": "0",
            }],
        }, format="json")
        self.assertEqual(invoice.status_code, 400, invoice.data)

    def test_service_acceptance_cannot_be_accepted_after_order_is_cancelled(self):
        service = Product.objects.create(
            company=self.company, unit=self.unit, code="SVC-CANCEL-2",
            name_ar="خدمة ملغى أمرها", product_type=Product.ProductType.SERVICE,
            stock_class=None, purchase_price=Decimal("750"),
        )
        order = self.client.post("/api/purchasing/orders/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "warehouse": self.warehouse.pk,
            "order_number": "PO-SVC-CANCEL-2", "order_date": "2026-03-10",
            "lines": [{"product": service.pk, "quantity": "1", "unit_price": "750", "tax_rate": "0"}],
        }, format="json")
        self.assertEqual(order.status_code, 201, order.data)
        confirmed = self.client.post(f"/api/purchasing/orders/{order.data['id']}/confirm/", {}, format="json")
        self.assertEqual(confirmed.status_code, 200, confirmed.data)
        acceptance = self.client.post("/api/purchasing/service-acceptances/", {
            "company": self.company.pk, "order": order.data["id"], "supplier": self.supplier.pk,
            "acceptance_number": "SA-SVC-CANCEL-2", "acceptance_date": "2026-03-11",
            "lines": [{"order_line": order.data["line_items"][0]["id"], "quantity": "1"}],
        }, format="json")
        self.assertEqual(acceptance.status_code, 201, acceptance.data)
        PurchaseOrder.objects.filter(pk=order.data["id"]).update(status=PurchaseOrder.Status.CANCELLED)
        rejected = self.client.post(
            f"/api/purchasing/service-acceptances/{acceptance.data['id']}/accept/", {}, format="json"
        )
        self.assertEqual(rejected.status_code, 400, rejected.data)
        self.assertEqual(
            PurchaseServiceAcceptance.objects.get(pk=acceptance.data["id"]).status,
            PurchaseServiceAcceptance.Status.DRAFT,
        )


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



    def test_landed_cost_automatic_value_and_quantity_allocation_preserves_exact_total(self):
        _, first_receipt_line_id = self._create_and_post_receipt()
        second_receipt = self.client.post("/api/purchasing/receipts/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "warehouse": self.warehouse.pk,
            "receipt_number": "GR-LC-AUTO-02", "receipt_date": "2026-03-11",
            "lines": [{"product": self.product.pk, "quantity": "3", "unit_cost": "50"}],
        }, format="json")
        self.assertEqual(second_receipt.status_code, 201, second_receipt.data)
        second_receipt_id = second_receipt.data["id"]
        second_receipt_line_id = second_receipt.data["line_items"][0]["id"]
        posted_receipt = self.client.post(
            f"/api/purchasing/receipts/{second_receipt_id}/post/", {}, format="json",
        )
        self.assertEqual(posted_receipt.status_code, 200, posted_receipt.data)

        expense = ChartOfAccount.objects.get(company=self.company, code="6000")
        source_response = self.client.post("/api/purchasing/invoices/", {
            "company": self.company.pk, "supplier": self.supplier.pk,
            "invoice_type": "direct_expense", "branch": self.branch.pk,
            "invoice_number": "LC-AUTO-SOURCE", "invoice_date": "2026-03-12",
            "lines": [{
                "description": "شحن",
                "expense_account": expense.pk,
                "billed_quantity": "1",
                "unit_price": "40.0000",
                "tax_rate": "0",
            }],
        }, format="json")
        self.assertEqual(source_response.status_code, 201, source_response.data)
        posted_invoice = self.client.post(
            f"/api/purchasing/invoices/{source_response.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(posted_invoice.status_code, 200, posted_invoice.data)
        source_line_id = PurchaseInvoice.objects.get(pk=source_response.data["id"]).lines.get().pk

        cases = (
            ("value", "LC-AUTO-VALUE", ["11.4286", "8.5714"]),
            ("quantity", "LC-AUTO-QUANTITY", ["8.0000", "12.0000"]),
        )
        for basis, document_number, expected in cases:
            with self.subTest(basis=basis):
                response = self.client.post("/api/purchasing/landed-costs/", {
                    "company": self.company.pk,
                    "source_invoice": source_response.data["id"],
                    "document_number": document_number,
                    "document_date": "2026-03-13",
                    "allocations": [{
                        "source_invoice_line": source_line_id,
                        "receipt_lines": [first_receipt_line_id, second_receipt_line_id],
                        "basis": basis,
                        "amount": "20.0000",
                    }],
                }, format="json")
                self.assertEqual(response.status_code, 201, response.data)
                actual = sorted(
                    (row["receipt_line"], row["amount"])
                    for row in response.data["allocation_lines"]
                )
                expected_rows = sorted([
                    (first_receipt_line_id, expected[0]),
                    (second_receipt_line_id, expected[1]),
                ])
                self.assertEqual(actual, expected_rows)
                self.assertEqual(
                    sum((Decimal(row["amount"]) for row in response.data["allocation_lines"]), Decimal("0")),
                    Decimal("20.0000"),
                )

    def test_landed_cost_posts_across_branches_without_duplicate_supplier_payable_and_can_reverse(self):
        receipt_id, receipt_line_id = self._create_and_post_receipt()
        second_branch = Branch.objects.create(
            company=self.company, code="LC-BR-02", name_ar="فرع رسملة ثان",
        )
        second_warehouse = Warehouse.objects.create(
            company=self.company, branch=second_branch, code="LC-WH-02", name_ar="مخزن رسملة ثان",
        )
        transfer = StockMovement.objects.create(
            company=self.company, movement_number="LC-XFER-01",
            movement_type=StockMovement.MovementType.TRANSFER, movement_date=date(2026, 3, 11),
            source_warehouse=self.warehouse, destination_warehouse=second_warehouse,
            created_by=self.user,
        )
        StockMovementLine.objects.create(movement=transfer, product=self.product, quantity=Decimal("1"))
        post_stock_movement(transfer.pk, self.user)

        issue = StockMovement.objects.create(
            company=self.company, movement_number="LC-ISS-01",
            movement_type=StockMovement.MovementType.ISSUE, movement_date=date(2026, 3, 12),
            source_warehouse=second_warehouse, created_by=self.user,
        )
        StockMovementLine.objects.create(movement=issue, product=self.product, quantity=Decimal("1"))
        post_stock_movement(issue.pk, self.user)

        expense = ChartOfAccount.objects.get(company=self.company, code="6000")
        source_response = self.client.post("/api/purchasing/invoices/", {
            "company": self.company.pk, "supplier": self.supplier.pk,
            "invoice_type": "direct_expense", "branch": self.branch.pk,
            "invoice_number": "LC-EXP-001", "invoice_date": "2026-03-13",
            "lines": [{
                "description": "شحن وجمارك",
                "expense_account": expense.pk,
                "billed_quantity": "1",
                "unit_price": "20.0000",
                "tax_rate": "0",
            }],
        }, format="json")
        self.assertEqual(source_response.status_code, 201, source_response.data)
        source_posted = self.client.post(
            f"/api/purchasing/invoices/{source_response.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(source_posted.status_code, 200, source_posted.data)
        source_invoice = PurchaseInvoice.objects.get(pk=source_response.data["id"])
        source_line = source_invoice.lines.get()

        created = self.client.post("/api/purchasing/landed-costs/", {
            "company": self.company.pk,
            "source_invoice": source_invoice.pk,
            "document_number": "LC-001",
            "document_date": "2026-03-14",
            "allocations": [{
                "source_invoice_line": source_line.pk,
                "receipt_line": receipt_line_id,
                "basis": "manual",
                "amount": "20.0000",
            }],
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)

        posted = self.client.post(
            f"/api/purchasing/landed-costs/{created.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(posted.status_code, 200, posted.data)
        document = LandedCostDocument.objects.get(pk=created.data["id"])
        self.assertEqual(document.status, LandedCostDocument.Status.POSTED)
        self.assertEqual(document.total_amount, Decimal("20.0000"))
        self.assertEqual(source_invoice.status, PurchaseInvoice.Status.POSTED)
        self.assertEqual(source_invoice.journal_entry.lines.get(
            account=AccountRole.objects.get(company=self.company, role=AccountRole.Role.ACCOUNTS_PAYABLE).account,
        ).credit, Decimal("20.0000"))

        source_balance = StockBalance.objects.get(product=self.product, warehouse=self.warehouse)
        self.assertEqual(source_balance.quantity, Decimal("1"))
        self.assertEqual(source_balance.inventory_value, Decimal("110.0000"))
        self.assertEqual(source_balance.average_cost, Decimal("110.000000"))

        allocation = LandedCostAllocation.objects.get(document=document)
        distributions = {
            row.branch_id: row for row in allocation.distributions.select_related("journal_entry")
        }
        self.assertEqual(distributions[self.branch.pk].inventory_amount, Decimal("10.0000"))
        self.assertEqual(distributions[self.branch.pk].cogs_amount, Decimal("0.0000"))
        self.assertEqual(distributions[second_branch.pk].cogs_amount, Decimal("10.0000"))
        self.assertEqual(
            distributions[second_branch.pk].journal_entry.lines.get(
                account=AccountRole.objects.get(company=self.company, role=AccountRole.Role.COGS).account,
            ).debit,
            Decimal("10.0000"),
        )
        clearing = AccountRole.objects.get(
            company=self.company, role=AccountRole.Role.INTER_BRANCH_CLEARING,
        ).account
        all_landed_entries = [allocation.source_journal_entry, *[
            row.journal_entry for row in distributions.values()
        ]]
        clearing_debit = sum(
            (entry.lines.filter(account=clearing).aggregate(total=Sum("debit"))["total"] or Decimal("0"))
            for entry in set(all_landed_entries)
        )
        clearing_credit = sum(
            (entry.lines.filter(account=clearing).aggregate(total=Sum("credit"))["total"] or Decimal("0"))
            for entry in set(all_landed_entries)
        )
        self.assertEqual(clearing_debit, Decimal("10.0000"))
        self.assertEqual(clearing_credit, Decimal("10.0000"))

        report = self.client.get(
            f"/api/purchasing/landed-costs/report/?company={self.company.pk}",
        )
        self.assertEqual(report.status_code, 200, report.data)
        self.assertEqual(report.data["totals"]["allocated"], "20.0000")
        self.assertEqual(report.data["totals"]["inventory"], "10.0000")
        self.assertEqual(report.data["totals"]["cogs"], "10.0000")
        excel = self.client.get(
            f"/api/purchasing/landed-costs/export-excel/?company={self.company.pk}",
        )
        self.assertEqual(excel.status_code, 200)
        self.assertEqual(
            excel["Content-Type"],
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

        reversed_response = self.client.post(
            f"/api/purchasing/landed-costs/{document.pk}/reverse/",
            {"entry_date": "2026-03-20"}, format="json",
        )
        self.assertEqual(reversed_response.status_code, 200, reversed_response.data)
        document.refresh_from_db()
        source_balance.refresh_from_db()
        self.assertEqual(document.status, LandedCostDocument.Status.REVERSED)
        self.assertEqual(source_balance.inventory_value, Decimal("100.0000"))
        self.assertEqual(source_balance.average_cost, Decimal("100.000000"))
        self.assertEqual(source_invoice.journal_entry.status, JournalEntry.Status.POSTED)
        allocation.source_journal_entry.refresh_from_db()
        self.assertEqual(allocation.source_journal_entry.status, JournalEntry.Status.REVERSED)
        reversed_report = self.client.get(
            f"/api/purchasing/landed-costs/report/?company={self.company.pk}",
        )
        self.assertEqual(reversed_report.status_code, 200, reversed_report.data)
        self.assertEqual(reversed_report.data["totals"]["allocated"], "0")
        self.assertEqual(reversed_report.data["totals"]["reversed"], "20.0000")

    def test_landed_cost_on_goods_returned_before_capitalization_is_expensed_not_lost_from_trace(self):
        receipt_id, receipt_line_id = self._create_and_post_receipt()
        invoice_response = self.client.post("/api/purchasing/invoices/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "receipt": receipt_id,
            "invoice_number": "INV-LC-RETURN-TRACE", "invoice_date": "2026-03-10",
            "lines": [{"receipt_line": receipt_line_id, "unit_price": "100", "tax_rate": "14"}],
        }, format="json")
        self.assertEqual(invoice_response.status_code, 201, invoice_response.data)
        posted_invoice = self.client.post(
            f"/api/purchasing/invoices/{invoice_response.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(posted_invoice.status_code, 200, posted_invoice.data)

        returned = self.client.post("/api/purchasing/returns/", {
            "company": self.company.pk, "supplier": self.supplier.pk,
            "invoice": invoice_response.data["id"], "return_number": "RET-LC-TRACE",
            "return_date": "2026-03-12",
            "lines": [{
                "invoice_line": invoice_response.data["invoice_lines"][0]["id"],
                "quantity": "1",
            }],
        }, format="json")
        self.assertEqual(returned.status_code, 201, returned.data)
        posted_return = self.client.post(
            f"/api/purchasing/returns/{returned.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(posted_return.status_code, 200, posted_return.data)

        expense = ChartOfAccount.objects.get(company=self.company, code="6000")
        source_response = self.client.post("/api/purchasing/invoices/", {
            "company": self.company.pk, "supplier": self.supplier.pk,
            "invoice_type": "direct_expense", "branch": self.branch.pk,
            "invoice_number": "LC-EXP-RETURN-TRACE", "invoice_date": "2026-03-13",
            "lines": [{
                "description": "شحن غير قابل للاسترداد",
                "expense_account": expense.pk,
                "billed_quantity": "1",
                "unit_price": "20.0000",
                "tax_rate": "0",
            }],
        }, format="json")
        self.assertEqual(source_response.status_code, 201, source_response.data)
        posted_expense = self.client.post(
            f"/api/purchasing/invoices/{source_response.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(posted_expense.status_code, 200, posted_expense.data)
        source_line = PurchaseInvoice.objects.get(pk=source_response.data["id"]).lines.get()

        created = self.client.post("/api/purchasing/landed-costs/", {
            "company": self.company.pk,
            "source_invoice": source_response.data["id"],
            "document_number": "LC-RETURN-TRACE",
            "document_date": "2026-03-14",
            "allocations": [{
                "source_invoice_line": source_line.pk,
                "receipt_line": receipt_line_id,
                "basis": "manual",
                "amount": "20.0000",
            }],
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        posted_landed_cost = self.client.post(
            f"/api/purchasing/landed-costs/{created.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(posted_landed_cost.status_code, 200, posted_landed_cost.data)

        allocation = LandedCostAllocation.objects.get(document_id=created.data["id"])
        distribution = allocation.distributions.get(branch=self.branch)
        self.assertEqual(distribution.inventory_amount, Decimal("10.0000"))
        self.assertEqual(distribution.cogs_amount, Decimal("0.0000"))
        self.assertEqual(distribution.adjustment_loss_amount, Decimal("10.0000"))
        balance = StockBalance.objects.get(product=self.product, warehouse=self.warehouse)
        self.assertEqual(balance.quantity, Decimal("1.0000"))
        self.assertEqual(balance.inventory_value, Decimal("110.0000"))
        loss_account = AccountRole.objects.get(
            company=self.company, role=AccountRole.Role.INVENTORY_ADJUSTMENT_LOSS,
        ).account
        self.assertEqual(
            distribution.journal_entry.lines.get(account=loss_account).debit,
            Decimal("10.0000"),
        )

        # Reversing capitalization after the earlier supplier return must restore
        # the remaining stock value and reverse the loss/reclassification journals.
        reversed_response = self.client.post(
            f"/api/purchasing/landed-costs/{created.data['id']}/reverse/",
            {"entry_date": "2026-03-15"}, format="json",
        )
        self.assertEqual(reversed_response.status_code, 200, reversed_response.data)
        document = LandedCostDocument.objects.get(pk=created.data["id"])
        allocation.refresh_from_db()
        distribution.refresh_from_db()
        balance.refresh_from_db()
        self.assertEqual(document.status, LandedCostDocument.Status.REVERSED)
        self.assertEqual(balance.quantity, Decimal("1.0000"))
        self.assertEqual(balance.inventory_value, Decimal("100.0000"))
        self.assertEqual(balance.average_cost, Decimal("100.000000"))
        self.assertEqual(distribution.journal_entry.status, JournalEntry.Status.REVERSED)
        self.assertIsNotNone(distribution.reversal_journal_entry_id)
        reversed_loss = distribution.reversal_journal_entry.lines.get(account=loss_account)
        self.assertEqual(reversed_loss.credit, Decimal("10.0000"))

        repeated_reverse = self.client.post(
            f"/api/purchasing/landed-costs/{created.data['id']}/reverse/",
            {"entry_date": "2026-03-16"}, format="json",
        )
        self.assertEqual(repeated_reverse.status_code, 400, repeated_reverse.data)


    def test_landed_cost_cannot_exceed_unallocated_direct_expense_line(self):
        _, receipt_line_id = self._create_and_post_receipt()
        expense = ChartOfAccount.objects.get(company=self.company, code="6000")
        source_response = self.client.post("/api/purchasing/invoices/", {
            "company": self.company.pk, "supplier": self.supplier.pk,
            "invoice_type": "direct_expense", "branch": self.branch.pk,
            "invoice_number": "LC-EXP-LIMIT", "invoice_date": "2026-03-13",
            "lines": [{
                "description": "شحن",
                "expense_account": expense.pk,
                "billed_quantity": "1",
                "unit_price": "20.0000",
                "tax_rate": "0",
            }],
        }, format="json")
        self.assertEqual(source_response.status_code, 201, source_response.data)
        self.client.post(
            f"/api/purchasing/invoices/{source_response.data['id']}/post/", {}, format="json",
        )
        source_line = PurchaseInvoice.objects.get(pk=source_response.data["id"]).lines.get()
        rejected = self.client.post("/api/purchasing/landed-costs/", {
            "company": self.company.pk,
            "source_invoice": source_response.data["id"],
            "document_number": "LC-OVER-001",
            "document_date": "2026-03-14",
            "allocations": [{
                "source_invoice_line": source_line.pk,
                "receipt_line": receipt_line_id,
                "basis": "manual",
                "amount": "20.0001",
            }],
        }, format="json")
        self.assertEqual(rejected.status_code, 400, rejected.data)
        self.assertFalse(LandedCostDocument.objects.filter(document_number="LC-OVER-001").exists())


    def test_landed_cost_reversal_is_blocked_after_later_stock_consumption(self):
        _, receipt_line_id = self._create_and_post_receipt()
        expense = ChartOfAccount.objects.get(company=self.company, code="6000")
        source_response = self.client.post("/api/purchasing/invoices/", {
            "company": self.company.pk, "supplier": self.supplier.pk,
            "invoice_type": "direct_expense", "branch": self.branch.pk,
            "invoice_number": "LC-EXP-LATER-ISSUE", "invoice_date": "2026-03-13",
            "lines": [{
                "description": "شحن",
                "expense_account": expense.pk,
                "billed_quantity": "1",
                "unit_price": "20.0000",
                "tax_rate": "0",
            }],
        }, format="json")
        self.assertEqual(source_response.status_code, 201, source_response.data)
        self.client.post(
            f"/api/purchasing/invoices/{source_response.data['id']}/post/", {}, format="json",
        )
        source_line = PurchaseInvoice.objects.get(pk=source_response.data["id"]).lines.get()
        created = self.client.post("/api/purchasing/landed-costs/", {
            "company": self.company.pk,
            "source_invoice": source_response.data["id"],
            "document_number": "LC-LATER-ISSUE",
            "document_date": "2026-03-14",
            "allocations": [{
                "source_invoice_line": source_line.pk,
                "receipt_line": receipt_line_id,
                "basis": "manual",
                "amount": "20.0000",
            }],
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        posted = self.client.post(
            f"/api/purchasing/landed-costs/{created.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(posted.status_code, 200, posted.data)

        issue = StockMovement.objects.create(
            company=self.company, movement_number="LC-LATER-ISSUE-01",
            movement_type=StockMovement.MovementType.ISSUE, movement_date=date(2026, 3, 16),
            source_warehouse=self.warehouse, created_by=self.user,
        )
        StockMovementLine.objects.create(movement=issue, product=self.product, quantity=Decimal("1"))
        post_stock_movement(issue.pk, self.user)

        rejected = self.client.post(
            f"/api/purchasing/landed-costs/{created.data['id']}/reverse/",
            {"entry_date": "2026-03-20"}, format="json",
        )
        self.assertEqual(rejected.status_code, 400, rejected.data)
        document = LandedCostDocument.objects.get(pk=created.data["id"])
        self.assertEqual(document.status, LandedCostDocument.Status.POSTED)
        balance = StockBalance.objects.get(product=self.product, warehouse=self.warehouse)
        self.assertEqual(balance.quantity, Decimal("1"))
        self.assertEqual(balance.inventory_value, Decimal("110.0000"))


    def test_purchase_matching_report_reconciles_order_receipt_and_partial_invoice_quantities(self):
        order_response = self.client.post("/api/purchasing/orders/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "warehouse": self.warehouse.pk,
            "order_number": "PO-MATCH-001", "order_date": "2026-03-01",
            "lines": [{"product": self.product.pk, "quantity": "10", "unit_price": "100", "tax_rate": "0"}],
        }, format="json")
        self.assertEqual(order_response.status_code, 201, order_response.data)
        order_id = order_response.data["id"]
        order_line_id = order_response.data["line_items"][0]["id"]
        confirmed = self.client.post(f"/api/purchasing/orders/{order_id}/confirm/", {}, format="json")
        self.assertEqual(confirmed.status_code, 200, confirmed.data)

        receipt = self.client.post("/api/purchasing/receipts/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "warehouse": self.warehouse.pk,
            "purchase_order": order_id, "receipt_number": "GR-MATCH-001", "receipt_date": "2026-03-05",
            "lines": [{
                "product": self.product.pk, "quantity": "4", "unit_cost": "100",
                "purchase_order_line": order_line_id,
            }],
        }, format="json")
        self.assertEqual(receipt.status_code, 201, receipt.data)
        posted_receipt = self.client.post(
            f"/api/purchasing/receipts/{receipt.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(posted_receipt.status_code, 200, posted_receipt.data)
        receipt_line_id = receipt.data["line_items"][0]["id"]

        invoice = self.client.post("/api/purchasing/invoices/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "receipt": receipt.data["id"],
            "invoice_number": "INV-MATCH-001", "invoice_date": "2026-03-06",
            "lines": [{
                "receipt_line": receipt_line_id, "billed_quantity": "2",
                "unit_price": "100", "tax_rate": "0",
            }],
        }, format="json")
        self.assertEqual(invoice.status_code, 201, invoice.data)
        posted_invoice = self.client.post(
            f"/api/purchasing/invoices/{invoice.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(posted_invoice.status_code, 200, posted_invoice.data)

        report = self.client.get(
            f"/api/purchasing/orders/matching-report/?company={self.company.pk}",
        )
        self.assertEqual(report.status_code, 200, report.data)
        row = next(item for item in report.data["rows"] if item["order_id"] == order_id)
        self.assertEqual(row["ordered_quantity"], "10.0000")
        self.assertEqual(row["received_quantity"], "4.0000")
        self.assertEqual(row["invoiced_quantity"], "2.0000")
        self.assertEqual(row["uninvoiced_received_quantity"], "2.0000")
        self.assertEqual(row["unreceived_quantity"], "6.0000")
        self.assertEqual(row["price_variance"], "0.0000")
        self.assertEqual(row["receipt_numbers"], ["GR-MATCH-001"])
        self.assertEqual(row["invoice_numbers"], ["INV-MATCH-001"])
        self.assertEqual(report.data["totals"]["order_value"], "1000.0000")
        self.assertEqual(report.data["totals"]["invoiced_net_amount"], "200.0000")
        excel = self.client.get(
            f"/api/purchasing/orders/matching-report-export-excel/?company={self.company.pk}",
        )
        self.assertEqual(excel.status_code, 200)
        self.assertEqual(
            excel["Content-Type"],
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )


    def test_purchase_analysis_report_groups_inventory_and_direct_expense_by_supplier(self):
        receipt_id, receipt_line_id = self._create_and_post_receipt()
        inventory_invoice = self.client.post("/api/purchasing/invoices/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "receipt": receipt_id,
            "invoice_number": "INV-ANALYSIS-001", "invoice_date": "2026-03-11",
            "lines": [{
                "receipt_line": receipt_line_id, "billed_quantity": "2",
                "unit_price": "100", "tax_rate": "14",
            }],
        }, format="json")
        self.assertEqual(inventory_invoice.status_code, 201, inventory_invoice.data)
        inventory_posted = self.client.post(
            f"/api/purchasing/invoices/{inventory_invoice.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(inventory_posted.status_code, 200, inventory_posted.data)

        expense = ChartOfAccount.objects.get(company=self.company, code="6000")
        direct_invoice = self.client.post("/api/purchasing/invoices/", {
            "company": self.company.pk, "supplier": self.supplier.pk,
            "invoice_type": "direct_expense", "branch": self.branch.pk,
            "invoice_number": "INV-ANALYSIS-002", "invoice_date": "2026-03-12",
            "lines": [{
                "description": "شحن",
                "expense_account": expense.pk,
                "billed_quantity": "1",
                "unit_price": "20",
                "tax_rate": "0",
            }],
        }, format="json")
        self.assertEqual(direct_invoice.status_code, 201, direct_invoice.data)
        direct_posted = self.client.post(
            f"/api/purchasing/invoices/{direct_invoice.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(direct_posted.status_code, 200, direct_posted.data)

        report = self.client.get(
            f"/api/purchasing/invoices/analysis-report/?company={self.company.pk}",
        )
        self.assertEqual(report.status_code, 200, report.data)
        self.assertEqual(report.data["totals"]["invoice_count"], 2)
        self.assertEqual(report.data["totals"]["line_count"], 2)
        self.assertEqual(report.data["totals"]["purchase_net"], "220.0000")
        self.assertEqual(report.data["totals"]["net_purchases"], "220.0000")
        self.assertEqual(report.data["totals"]["tax_amount"], "28.0000")
        self.assertEqual(
            {row["kind"] for row in report.data["rows"]},
            {"inventory", "direct_expense"},
        )
        excel = self.client.get(
            f"/api/purchasing/invoices/analysis-report-export-excel/?company={self.company.pk}",
        )
        self.assertEqual(excel.status_code, 200)
        self.assertEqual(
            excel["Content-Type"],
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    def test_supplier_aging_excludes_credit_balance_after_return_refund_and_reversal(self):
        receipt_id, receipt_line_id = self._create_and_post_receipt()
        invoice = self.client.post("/api/purchasing/invoices/", {
            "company": self.company.pk, "supplier": self.supplier.pk,
            "receipt": receipt_id, "invoice_number": "INV-AGE-REFUND-001",
            "invoice_date": "2026-03-10",
            "lines": [{
                "receipt_line": receipt_line_id, "billed_quantity": "2",
                "unit_price": "100", "tax_rate": "14",
            }],
        }, format="json")
        self.assertEqual(invoice.status_code, 201, invoice.data)
        posted_invoice = self.client.post(
            f"/api/purchasing/invoices/{invoice.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(posted_invoice.status_code, 200, posted_invoice.data)

        # Refunds require an actual supplier credit: settle the invoice first,
        # then the posted return creates the credit balance to be refunded.
        bank = ChartOfAccount.objects.create(
            company=self.company, code="1118", name_ar="بنك اختبار أعمار المورد",
            nature=ChartOfAccount.Nature.ASSET,
        )
        TreasuryAccount.objects.create(
            company=self.company, branch=self.branch, account=bank,
            kind=TreasuryAccount.Kind.BANK, code="BANK-AGE-REFUND",
            name_ar="بنك اختبار أعمار المورد", currency_code="EGP",
            opening_balance=Decimal("228"),
        )
        payment = self.client.post("/api/purchasing/payments/", {
            "company": self.company.pk, "supplier": self.supplier.pk,
            "invoice": invoice.data["id"], "payment_number": "PAY-AGE-REFUND-001",
            "payment_date": "2026-03-11", "method": "bank",
            "payment_account": bank.pk, "amount": "228",
        }, format="json")
        self.assertEqual(payment.status_code, 201, payment.data)
        posted_payment = self.client.post(
            f"/api/purchasing/payments/{payment.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(posted_payment.status_code, 200, posted_payment.data)

        returned = self.client.post("/api/purchasing/returns/", {
            "company": self.company.pk, "supplier": self.supplier.pk,
            "invoice": invoice.data["id"], "return_number": "RET-AGE-REFUND-001",
            "return_date": "2026-03-12",
            "lines": [{
                "invoice_line": invoice.data["invoice_lines"][0]["id"],
                "quantity": "1",
            }],
        }, format="json")
        self.assertEqual(returned.status_code, 201, returned.data)
        posted_return = self.client.post(
            f"/api/purchasing/returns/{returned.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(posted_return.status_code, 200, posted_return.data)

        bank = AccountRole.objects.get(
            company=self.company, role=AccountRole.Role.BANK,
        ).account
        refund = self.client.post("/api/purchasing/refunds/", {
            "company": self.company.pk, "supplier": self.supplier.pk,
            "return_document": posted_return.data["id"],
            "refund_number": "REF-AGE-REFUND-001", "refund_date": "2026-03-13",
            "method": "bank", "payment_account": bank.pk, "amount": "50",
        }, format="json")
        self.assertEqual(refund.status_code, 201, refund.data)
        posted_refund = self.client.post(
            f"/api/purchasing/refunds/{refund.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(posted_refund.status_code, 200, posted_refund.data)
        invoice_detail = self.client.get(
            f"/api/purchasing/invoices/{invoice.data['id']}/",
        )
        self.assertEqual(invoice_detail.status_code, 200, invoice_detail.data)
        self.assertEqual(Decimal(invoice_detail.data["balance_due"]), Decimal("0"))

        before_reversal = self.client.get(
            "/api/purchasing/invoices/supplier-aging/",
            {"company": self.company.pk, "as_of": "2026-03-14"},
        )
        self.assertEqual(before_reversal.status_code, 200, before_reversal.data)
        self.assertEqual(Decimal(before_reversal.data["totals"]["total_due"]), Decimal("0"))

        reversed_refund = self.client.post(
            f"/api/purchasing/refunds/{refund.data['id']}/reverse/",
            {"entry_date": "2026-03-16"}, format="json",
        )
        self.assertEqual(reversed_refund.status_code, 200, reversed_refund.data)
        after_reversal = self.client.get(
            "/api/purchasing/invoices/supplier-aging/",
            {"company": self.company.pk, "as_of": "2026-03-17"},
        )
        self.assertEqual(after_reversal.status_code, 200, after_reversal.data)
        self.assertEqual(Decimal(after_reversal.data["totals"]["total_due"]), Decimal("0"))

class PurchaseReceiptConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.client = APIClient()
        PurchasingWorkflowTests.setUp(self)

    def test_concurrent_receipts_respect_reserved_purchase_order_quantity(self):
        if connection.vendor != "postgresql":
            self.skipTest("Concurrent row-locking regression requires PostgreSQL.")

        order = self.client.post("/api/purchasing/orders/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "warehouse": self.warehouse.pk,
            "order_number": "PO-CONCURRENT-RECEIPT", "order_date": "2026-03-01",
            "lines": [{"product": self.product.pk, "quantity": "10", "unit_price": "100", "tax_rate": "14"}],
        }, format="json")
        self.assertEqual(order.status_code, 201, order.data)
        order_id = order.data["id"]
        order_line_id = order.data["line_items"][0]["id"]
        confirmed = self.client.post(f"/api/purchasing/orders/{order_id}/confirm/", {}, format="json")
        self.assertEqual(confirmed.status_code, 200, confirmed.data)

        receipt_ids = []
        for number, quantity in (("GR-CONCURRENT-A", "6"), ("GR-CONCURRENT-B", "4")):
            receipt = self.client.post("/api/purchasing/receipts/", {
                "company": self.company.pk, "supplier": self.supplier.pk, "warehouse": self.warehouse.pk,
                "purchase_order": order_id, "receipt_number": number, "receipt_date": "2026-03-10",
                "lines": [{
                    "product": self.product.pk, "quantity": quantity, "unit_cost": "100",
                    "purchase_order_line": order_line_id,
                }],
            }, format="json")
            self.assertEqual(receipt.status_code, 201, receipt.data)
            receipt_ids.append(receipt.data["id"])

        # The draft-receipt reservation must reject any additional quantity
        # before posting, while the two valid receipts are posted concurrently.
        excess = self.client.post("/api/purchasing/receipts/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "warehouse": self.warehouse.pk,
            "purchase_order": order_id, "receipt_number": "GR-CONCURRENT-EXCESS", "receipt_date": "2026-03-10",
            "lines": [{
                "product": self.product.pk, "quantity": "1", "unit_cost": "100",
                "purchase_order_line": order_line_id,
            }],
        }, format="json")
        self.assertEqual(excess.status_code, 400, excess.data)

        barrier = threading.Barrier(2)

        def post_receipt(receipt_id):
            from django.db import close_old_connections
            close_old_connections()
            client = APIClient()
            user = get_user_model().objects.get(pk=self.user.pk)
            client.force_authenticate(user)
            barrier.wait(timeout=10)
            try:
                response = client.post(f"/api/purchasing/receipts/{receipt_id}/post/", {}, format="json")
                return response.status_code, response.data
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(post_receipt, receipt_ids))

        self.assertEqual([status for status, _ in results].count(200), 2, results)
        order_line = PurchaseOrderReceiptLine.objects.filter(order_line_id=order_line_id).aggregate(
            total=Sum("quantity")
        )["total"]
        self.assertEqual(order_line, Decimal("10"))
        posted_receipts = StockMovement.objects.filter(
            movement_number__in=("PR-GR-CONCURRENT-A", "PR-GR-CONCURRENT-B"),
            status=StockMovement.Status.POSTED,
        ).count()
        self.assertEqual(posted_receipts, 2)
        balance = StockBalance.objects.get(product=self.product, warehouse=self.warehouse)
        self.assertEqual(balance.quantity, Decimal("10"))


class PurchaseInvoiceIntegrityTests(TransactionTestCase):
    def setUp(self):
        self.client = APIClient()
        PurchasingWorkflowTests.setUp(self)

    def test_invoice_posting_rolls_back_posted_journal_when_later_step_fails(self):
        receipt_id, receipt_line_id = PurchasingWorkflowTests._create_and_post_receipt(self)
        invoice = self.client.post("/api/purchasing/invoices/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "receipt": receipt_id,
            "invoice_number": "INV-ATOMIC-ROLLBACK", "invoice_date": "2026-03-10",
            "lines": [{
                "receipt_line": receipt_line_id, "unit_price": "100", "tax_rate": "14",
            }],
        }, format="json")
        self.assertEqual(invoice.status_code, 201, invoice.data)
        invoice_id = invoice.data["id"]
        journal_count_before = JournalEntry.objects.count()

        from purchasing import services as purchasing_services
        original_post_journal = purchasing_services.post_journal

        def post_then_fail(entry_id, actor=None):
            original_post_journal(entry_id, actor=actor)
            raise DRFValidationError("Injected failure after journal posting.")

        with patch("purchasing.services.post_journal", side_effect=post_then_fail):
            response = self.client.post(
                f"/api/purchasing/invoices/{invoice_id}/post/", {}, format="json",
            )

        self.assertEqual(response.status_code, 400, response.data)
        saved_invoice = PurchaseInvoice.objects.get(pk=invoice_id)
        self.assertEqual(saved_invoice.status, PurchaseInvoice.Status.DRAFT)
        self.assertIsNone(saved_invoice.journal_entry_id)
        self.assertEqual(JournalEntry.objects.count(), journal_count_before)
        self.assertFalse(JournalEntry.objects.filter(
            entry_number=f"PUR-INV-{invoice_id}",
        ).exists())
        self.assertEqual(
            StockBalance.objects.get(product=self.product, warehouse=self.warehouse).quantity,
            Decimal("2"),
        )

    def test_concurrent_partial_invoices_serialize_grni_consumption(self):
        if connection.vendor != "postgresql":
            self.skipTest("Concurrent invoice row-locking regression requires PostgreSQL.")

        receipt_id, receipt_line_id = PurchasingWorkflowTests._create_and_post_receipt(self)
        invoice_ids = []
        for number in ("INV-CONCURRENT-PART-A", "INV-CONCURRENT-PART-B"):
            invoice = self.client.post("/api/purchasing/invoices/", {
                "company": self.company.pk, "supplier": self.supplier.pk, "receipt": receipt_id,
                "invoice_number": number, "invoice_date": "2026-03-10",
                "lines": [{
                    "receipt_line": receipt_line_id, "billed_quantity": "1",
                    "unit_price": "100", "tax_rate": "14",
                }],
            }, format="json")
            self.assertEqual(invoice.status_code, 201, invoice.data)
            invoice_ids.append(invoice.data["id"])

        barrier = threading.Barrier(2)

        def post_invoice(invoice_id):
            from django.db import close_old_connections
            close_old_connections()
            client = APIClient()
            user = get_user_model().objects.get(pk=self.user.pk)
            client.force_authenticate(user)
            barrier.wait(timeout=10)
            try:
                response = client.post(
                    f"/api/purchasing/invoices/{invoice_id}/post/", {}, format="json",
                )
                return response.status_code, response.data
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(post_invoice, invoice_ids))

        self.assertEqual([status for status, _ in results].count(200), 2, results)
        posted_invoices = list(PurchaseInvoice.objects.filter(pk__in=invoice_ids))
        self.assertEqual(
            {invoice.status for invoice in posted_invoices},
            {PurchaseInvoice.Status.POSTED},
        )
        self.assertEqual(
            PurchaseInvoiceLine.objects.filter(
                receipt_line_id=receipt_line_id,
                invoice__status=PurchaseInvoice.Status.POSTED,
            ).aggregate(total=Sum("billed_quantity"))["total"],
            Decimal("2"),
        )
        self.assertEqual(
            JournalEntry.objects.filter(
                source_type=JournalEntry.Source.PURCHASES, status=JournalEntry.Status.POSTED,
            ).count(),
            2,
        )
        grni = self.client.get(
            "/api/purchasing/receipts/grni-aging/",
            {"company": self.company.pk, "as_of": "2026-03-10"},
        )
        self.assertEqual(grni.status_code, 200, grni.data)
        self.assertEqual(grni.data["grand_total"], "0.0000")
        self.assertEqual(grni.data["gl_reconciliation"]["difference"], "0.0000")
        self.assertTrue(grni.data["gl_reconciliation"]["reconciled"])



class SupplierPaymentIntegrityTests(TransactionTestCase):
    """Atomicity and PostgreSQL concurrency regressions for supplier disbursements."""

    def setUp(self):
        self.client = APIClient()
        PurchasingWorkflowTests.setUp(self)

    def _create_posted_invoice(self, invoice_number):
        receipt_id, receipt_line_id = PurchasingWorkflowTests._create_and_post_receipt(self)
        invoice = self.client.post("/api/purchasing/invoices/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "receipt": receipt_id,
            "invoice_number": invoice_number, "invoice_date": "2026-03-10",
            "lines": [{
                "receipt_line": receipt_line_id, "unit_price": "100", "tax_rate": "14",
            }],
        }, format="json")
        self.assertEqual(invoice.status_code, 201, invoice.data)
        posted = self.client.post(
            f"/api/purchasing/invoices/{invoice.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(posted.status_code, 200, posted.data)
        return invoice.data["id"]

    def _create_payment(self, invoice_id, number, amount="114.0000"):
        bank = AccountRole.objects.get(
            company=self.company, role=AccountRole.Role.BANK,
        ).account
        response = self.client.post("/api/purchasing/payments/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "invoice": invoice_id,
            "payment_number": number, "payment_date": "2026-03-15",
            "method": "bank", "payment_account": bank.pk, "amount": amount,
            "reference": number,
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        return response.data["id"]

    def test_bank_payment_rolls_back_journal_and_treasury_movement_together(self):
        invoice_id = self._create_posted_invoice("INV-PAY-ATOMIC")
        payment_id = self._create_payment(invoice_id, "PAY-ATOMIC")
        journal_count = JournalEntry.objects.count()
        treasury_count = TreasuryTransaction.objects.count()

        from treasury.services import record_posted_document_bank_movement
        from purchasing.services import post_supplier_payment
        def create_then_fail(**kwargs):
            record_posted_document_bank_movement_original(**kwargs)
            raise DRFValidationError("Injected failure after treasury movement.")

        record_posted_document_bank_movement_original = record_posted_document_bank_movement
        with patch(
            "treasury.services.record_posted_document_bank_movement",
            side_effect=create_then_fail,
        ):
            with self.assertRaises(DRFValidationError):
                post_supplier_payment(payment_id, self.user)

        payment = SupplierPayment.objects.get(pk=payment_id)
        self.assertEqual(payment.status, SupplierPayment.Status.DRAFT)
        self.assertIsNone(payment.journal_entry_id)
        self.assertEqual(JournalEntry.objects.count(), journal_count)
        self.assertEqual(TreasuryTransaction.objects.count(), treasury_count)
        self.assertFalse(JournalEntry.objects.filter(
            entry_number=f"PUR-PAY-{payment_id}",
        ).exists())

    def test_concurrent_bank_payments_cannot_overpay_same_supplier_invoice(self):
        if connection.vendor != "postgresql":
            self.skipTest("Concurrent invoice row-locking regression requires PostgreSQL.")

        invoice_id = self._create_posted_invoice("INV-PAY-CONCURRENT")
        payment_ids = [
            self._create_payment(invoice_id, "PAY-CONCURRENT-A"),
            self._create_payment(invoice_id, "PAY-CONCURRENT-B"),
        ]
        barrier = threading.Barrier(2)

        def post_payment(payment_id):
            from django.db import close_old_connections
            close_old_connections()
            user = get_user_model().objects.get(pk=self.user.pk)
            barrier.wait(timeout=10)
            try:
                from purchasing.services import post_supplier_payment
                post_supplier_payment(payment_id, user)
                return "posted"
            except DRFValidationError:
                return "rejected"
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(post_payment, payment_ids))

        self.assertEqual(outcomes.count("posted"), 2, outcomes)
        payments = list(SupplierPayment.objects.filter(pk__in=payment_ids))
        self.assertTrue(all(p.status == SupplierPayment.Status.POSTED for p in payments))
        self.assertEqual(
            sum((p.amount for p in payments), Decimal("0")),
            Decimal("228.0000"),
        )
        bank = AccountRole.objects.get(
            company=self.company, role=AccountRole.Role.BANK,
        ).account
        self.assertEqual(
            JournalLine.objects.filter(
                account=bank, entry__status=JournalEntry.Status.POSTED,
                entry__entry_number__startswith="PUR-PAY-",
            ).aggregate(total=Sum("credit"))["total"],
            Decimal("228.0000"),
        )
        movements = TreasuryTransaction.objects.filter(
            document_number__startswith="AUTO-PUR-PAY-",
            status=TreasuryTransaction.Status.POSTED,
        )
        self.assertEqual(movements.count(), 2)
        self.assertEqual(
            movements.aggregate(total=Sum("amount"))["total"],
            Decimal("228.0000"),
        )



class SupplierReturnIntegrityTests(TransactionTestCase):
    """Atomicity and PostgreSQL concurrency regressions for supplier returns."""

    def setUp(self):
        self.client = APIClient()
        PurchasingWorkflowTests.setUp(self)

    def _create_posted_invoice(self, invoice_number):
        receipt_id, receipt_line_id = PurchasingWorkflowTests._create_and_post_receipt(self)
        invoice = self.client.post("/api/purchasing/invoices/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "receipt": receipt_id,
            "invoice_number": invoice_number, "invoice_date": "2026-03-10",
            "lines": [{
                "receipt_line": receipt_line_id, "unit_price": "100", "tax_rate": "14",
            }],
        }, format="json")
        self.assertEqual(invoice.status_code, 201, invoice.data)
        posted = self.client.post(
            f"/api/purchasing/invoices/{invoice.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(posted.status_code, 200, posted.data)
        return invoice.data

    def _create_return(self, invoice, number, quantity="1"):
        response = self.client.post("/api/purchasing/returns/", {
            "company": self.company.pk, "supplier": self.supplier.pk,
            "invoice": invoice["id"], "return_number": number,
            "return_date": "2026-03-12",
            "lines": [{
                "invoice_line": invoice["invoice_lines"][0]["id"],
                "quantity": quantity,
            }],
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        return response.data["id"]

    def test_supplier_return_rolls_back_stock_and_journal_when_posting_fails(self):
        invoice = self._create_posted_invoice("INV-RETURN-ATOMIC")
        return_id = self._create_return(invoice, "RET-ATOMIC")
        return_document = SupplierReturn.objects.get(pk=return_id)
        movement_id = return_document.stock_movement_id
        journal_count_before = JournalEntry.objects.count()
        balance_before = StockBalance.objects.get(
            product=self.product, warehouse=self.warehouse,
        )
        self.assertEqual(balance_before.quantity, Decimal("2.0000"))
        self.assertEqual(balance_before.inventory_value, Decimal("200.0000"))

        from purchasing import services as purchasing_services
        original_post_journal = purchasing_services.post_journal

        def post_then_fail(entry_id, actor=None):
            original_post_journal(entry_id, actor=actor)
            raise DRFValidationError("Injected failure after supplier-return journal posting.")

        with patch("purchasing.services.post_journal", side_effect=post_then_fail):
            response = self.client.post(
                f"/api/purchasing/returns/{return_id}/post/", {}, format="json",
            )

        self.assertEqual(response.status_code, 400, response.data)
        return_document.refresh_from_db()
        movement = StockMovement.objects.get(pk=movement_id)
        balance_after = StockBalance.objects.get(
            product=self.product, warehouse=self.warehouse,
        )
        self.assertEqual(return_document.status, SupplierReturn.Status.DRAFT)
        self.assertIsNone(return_document.journal_entry_id)
        self.assertEqual(movement.status, StockMovement.Status.DRAFT)
        self.assertEqual(balance_after.quantity, balance_before.quantity)
        self.assertEqual(balance_after.inventory_value, balance_before.inventory_value)
        self.assertEqual(JournalEntry.objects.count(), journal_count_before)
        self.assertFalse(JournalEntry.objects.filter(
            entry_number=f"PUR-RET-{return_id}",
        ).exists())

    def test_concurrent_partial_supplier_returns_serialize_invoice_quantity_and_ap(self):
        if connection.vendor != "postgresql":
            self.skipTest("Concurrent supplier-return row-locking regression requires PostgreSQL.")

        from purchasing.models import SupplierReturnLine

        invoice = self._create_posted_invoice("INV-RETURN-CONCURRENT")
        return_ids = [
            self._create_return(invoice, "RET-CONCURRENT-A", "1"),
            self._create_return(invoice, "RET-CONCURRENT-B", "1"),
        ]
        barrier = threading.Barrier(2)

        def post_return(return_id):
            from django.db import close_old_connections
            close_old_connections()
            client = APIClient()
            user = get_user_model().objects.get(pk=self.user.pk)
            client.force_authenticate(user)
            barrier.wait(timeout=10)
            try:
                response = client.post(
                    f"/api/purchasing/returns/{return_id}/post/", {}, format="json",
                )
                return response.status_code, response.data
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(post_return, return_ids))

        self.assertEqual([status for status, _ in results].count(200), 2, results)
        returns = list(SupplierReturn.objects.filter(pk__in=return_ids))
        self.assertEqual(
            {document.status for document in returns},
            {SupplierReturn.Status.POSTED},
        )
        self.assertEqual(
            SupplierReturnLine.objects.filter(
                return_document__invoice_id=invoice["id"],
                return_document__status=SupplierReturn.Status.POSTED,
            ).aggregate(total=Sum("quantity"))["total"],
            Decimal("2.0000"),
        )
        balance = StockBalance.objects.get(product=self.product, warehouse=self.warehouse)
        self.assertEqual(balance.quantity, Decimal("0.0000"))
        self.assertEqual(balance.inventory_value, Decimal("0.0000"))

        invoice_data = self.client.get(
            f"/api/purchasing/invoices/{invoice['id']}/",
        ).data
        self.assertEqual(invoice_data["balance_due"], "0.0000")
        statement = self.client.get(
            "/api/purchasing/invoices/supplier-statement/",
            {"company": self.company.pk, "supplier": self.supplier.pk},
        )
        self.assertEqual(statement.status_code, 200, statement.data)
        self.assertEqual(statement.data["closing_balance"], "0.0000")

        ap_totals = JournalLine.objects.filter(
            account__code="2200", entry__company=self.company,
            entry__status=JournalEntry.Status.POSTED,
        ).aggregate(debit=Sum("debit"), credit=Sum("credit"))
        self.assertEqual(ap_totals["debit"], Decimal("228.0000"))
        self.assertEqual(ap_totals["credit"], Decimal("228.0000"))
        self.assertEqual(
            JournalLine.objects.filter(
                entry__in=[document.journal_entry for document in returns],
                account__code="1000", credit=Decimal("100.0000"),
            ).count(),
            2,
        )
        self.assertEqual(
            JournalLine.objects.filter(
                entry__in=[document.journal_entry for document in returns],
                account__code="1300", credit=Decimal("14.0000"),
            ).count(),
            2,
        )
