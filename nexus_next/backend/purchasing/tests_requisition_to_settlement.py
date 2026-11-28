from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db.models import Sum
from rest_framework.test import APITestCase

from accounting.models import AccountRole, ChartOfAccount, JournalEntry, JournalLine
from companies.models import (
    Branch, Company, CompanyMembership, Currency, FiscalYear,
)
from inventory.models import Product, StockBalance, UnitOfMeasure, Warehouse
from suppliers.models import Supplier
from customers.models import Customer
from treasury.models import TreasuryAccount, TreasuryTransaction


class PurchasingRequisitionToSettlementE2ETests(APITestCase):
    """One API journey from approved need through competition to settlement."""

    def setUp(self):
        currency = Currency.objects.create(code="EGP", name_ar="جنيه مصري")
        self.company = Company.objects.create(
            code="REQE2E", name_ar="شركة اختبار دورة المشتريات",
            country="EG", activity="commercial", base_currency=currency,
        )
        self.branch = Branch.objects.create(
            company=self.company, code="MAIN", name_ar="الرئيسي",
            is_head_office=True,
        )
        today = date.today()
        self.request_date = today
        self.rfq_issue_date = today + timedelta(days=1)
        self.rfq_close_date = today + timedelta(days=7)
        self.quote_date = today + timedelta(days=2)
        self.order_date = today + timedelta(days=3)
        self.receipt_date = today + timedelta(days=4)
        self.invoice_date = today + timedelta(days=5)
        self.payment_date = today + timedelta(days=6)
        self.return_date = today + timedelta(days=7)
        FiscalYear.objects.create(
            company=self.company, name=str(today.year),
            start_date=date(today.year, 1, 1),
            end_date=date(today.year, 12, 31),
        )
        user_model = get_user_model()
        self.buyer = user_model.objects.create_user(
            username="requisition-e2e-buyer", password="test-password",
        )
        self.manager = user_model.objects.create_user(
            username="requisition-e2e-manager", password="test-password",
        )
        CompanyMembership.objects.create(
            user=self.buyer, company=self.company,
            role="accountant", all_branches=True,
        )
        CompanyMembership.objects.create(
            user=self.manager, company=self.company,
            role="admin", all_branches=True,
        )
        self.client.force_authenticate(self.buyer)

        self.unit = UnitOfMeasure.objects.create(
            company=self.company, code="PCS", name_ar="قطعة",
        )
        self.product = Product.objects.create(
            company=self.company, unit=self.unit, code="REQ-E2E-1",
            name_ar="صنف دورة المشتريات", purchase_price=Decimal("100"),
        )
        self.warehouse = Warehouse.objects.create(
            company=self.company, branch=self.branch,
            code="WH1", name_ar="المخزن الرئيسي",
        )
        self.supplier_a = Supplier.objects.create(
            company=self.company, code="REQ-E2E-A", name_ar="المورد أ",
        )
        self.supplier_b = Supplier.objects.create(
            company=self.company, code="REQ-E2E-B", name_ar="المورد ب",
        )
        self.customer = Customer.objects.create(
            company=self.company, code="REQ-E2E-CUS", name_ar="عميل دورة التكامل",
        )

        roles = {
            AccountRole.Role.INVENTORY: ("1000", "المخزون", ChartOfAccount.Nature.ASSET),
            AccountRole.Role.COGS: ("5100", "تكلفة المبيعات", ChartOfAccount.Nature.EXPENSE),
            AccountRole.Role.INVENTORY_ADJUSTMENT_LOSS: ("5900", "خسائر تسوية المخزون", ChartOfAccount.Nature.EXPENSE),
            AccountRole.Role.INTER_BRANCH_CLEARING: ("2190", "تسوية بين الفروع", ChartOfAccount.Nature.LIABILITY),
            AccountRole.Role.GOODS_RECEIVED_NOT_INVOICED: ("2100", "بضاعة مستلمة غير مفوترة", ChartOfAccount.Nature.LIABILITY),
            AccountRole.Role.ACCOUNTS_PAYABLE: ("2200", "الموردون", ChartOfAccount.Nature.LIABILITY),
            AccountRole.Role.ACCOUNTS_RECEIVABLE: ("1200", "العملاء", ChartOfAccount.Nature.ASSET),
            AccountRole.Role.SALES: ("4000", "المبيعات", ChartOfAccount.Nature.REVENUE),
            AccountRole.Role.SALES_RETURNS: ("4090", "مردودات المبيعات", ChartOfAccount.Nature.REVENUE),
            AccountRole.Role.VAT_OUTPUT: ("2300", "ضريبة مخرجات", ChartOfAccount.Nature.LIABILITY),
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
            account = ChartOfAccount.objects.create(
                company=self.company, code=code, name_ar=name, nature=nature,
            )
            AccountRole.objects.create(
                company=self.company, role=role, account=account,
            )
        self.bank = AccountRole.objects.get(
            company=self.company, role=AccountRole.Role.BANK,
        ).account
        TreasuryAccount.objects.create(
            company=self.company, branch=self.branch, account=self.bank,
            kind=TreasuryAccount.Kind.BANK, code="BANK-E2E",
            name_ar="بنك الاختبار", currency_code="EGP",
            opening_balance=Decimal("100000"),
        )

    def test_approved_requisition_rfq_award_to_receipt_invoice_payment_and_return(self):
        requisition = self.client.post("/api/purchasing/requisitions/", {
            "company": self.company.pk,
            "request_number": "REQ-E2E-001",
            "request_date": self.request_date.isoformat(),
            "required_date": self.rfq_close_date.isoformat(),
            "department": "المخازن",
            "purpose": "احتياج تشغيلي لاختبار دورة الشراء كاملة",
            "lines": [{
                "product": self.product.pk,
                "requested_quantity": "4",
                "estimated_unit_price": "100",
            }],
        }, format="json")
        self.assertEqual(requisition.status_code, 201, requisition.data)
        requisition_id = requisition.data["id"]
        submitted = self.client.post(
            f"/api/purchasing/requisitions/{requisition_id}/submit/", {},
            format="json",
        )
        self.assertEqual(submitted.status_code, 200, submitted.data)

        self.client.force_authenticate(self.manager)
        reviewed = self.client.post(
            f"/api/purchasing/requisitions/{requisition_id}/review/",
            {"decision": "approve", "note": "اعتماد الاحتياج"},
            format="json",
        )
        self.assertEqual(reviewed.status_code, 200, reviewed.data)
        self.client.force_authenticate(self.buyer)

        rfq = self.client.post("/api/purchasing/rfqs/", {
            "company": self.company.pk,
            "source_requisition": requisition_id,
            "rfq_number": "RFQ-E2E-001",
            "issue_date": self.rfq_issue_date.isoformat(),
            "closing_date": self.rfq_close_date.isoformat(),
            "invited_suppliers": [self.supplier_a.pk, self.supplier_b.pk],
            "lines": [{"product": self.product.pk, "quantity": "4"}],
        }, format="json")
        self.assertEqual(rfq.status_code, 201, rfq.data)
        rfq_id = rfq.data["id"]
        rfq_line_id = rfq.data["line_items"][0]["id"]
        opened = self.client.post(
            f"/api/purchasing/rfqs/{rfq_id}/open/", {}, format="json",
        )
        self.assertEqual(opened.status_code, 200, opened.data)

        quote_a = self.client.post("/api/purchasing/quotations/", {
            "rfq": rfq_id, "supplier": self.supplier_a.pk,
            "quotation_number": "QUOTE-E2E-A",
            "quotation_date": self.quote_date.isoformat(), "valid_until": self.rfq_close_date.isoformat(),
            "delivery_days": 5, "payment_terms": "30 يوم",
            "lines": [{
                "rfq_line": rfq_line_id, "unit_price": "100",
                "discount_percent": "0", "tax_rate": "14",
            }],
        }, format="json")
        self.assertEqual(quote_a.status_code, 201, quote_a.data)
        quote_b = self.client.post("/api/purchasing/quotations/", {
            "rfq": rfq_id, "supplier": self.supplier_b.pk,
            "quotation_number": "QUOTE-E2E-B",
            "quotation_date": self.quote_date.isoformat(), "valid_until": self.rfq_close_date.isoformat(),
            "delivery_days": 8, "payment_terms": "نقدي",
            "lines": [{
                "rfq_line": rfq_line_id, "unit_price": "105",
                "discount_percent": "0", "tax_rate": "14",
            }],
        }, format="json")
        self.assertEqual(quote_b.status_code, 201, quote_b.data)
        self.assertLess(
            Decimal(quote_a.data["grand_total"]),
            Decimal(quote_b.data["grand_total"]),
        )

        self.client.force_authenticate(self.manager)
        awarded = self.client.post(f"/api/purchasing/rfqs/{rfq_id}/award/", {
            "quotation": quote_a.data["id"],
            "reason": "اختيار أقل عرض مستوفٍ للشروط",
        }, format="json")
        self.assertEqual(awarded.status_code, 200, awarded.data)
        converted = self.client.post(
            f"/api/purchasing/rfqs/{rfq_id}/convert-award/",
            {
                "warehouse": self.warehouse.pk,
                "order_number": "PO-E2E-001",
                "order_date": self.order_date.isoformat(),
                "expected_date": self.rfq_close_date.isoformat(),
            }, format="json",
        )
        self.assertEqual(converted.status_code, 201, converted.data)
        order_id = converted.data["purchase_order_id"]
        order = self.client.get(f"/api/purchasing/orders/{order_id}/")
        self.assertEqual(order.status_code, 200, order.data)
        order_line_id = order.data["line_items"][0]["id"]
        self.client.force_authenticate(self.buyer)
        confirmed = self.client.post(
            f"/api/purchasing/orders/{order_id}/confirm/", {}, format="json",
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.data)

        receipt = self.client.post("/api/purchasing/receipts/", {
            "company": self.company.pk, "supplier": self.supplier_a.pk,
            "warehouse": self.warehouse.pk, "purchase_order": order_id,
            "receipt_number": "GR-E2E-001", "receipt_date": self.receipt_date.isoformat(),
            "lines": [{
                "product": self.product.pk, "quantity": "2",
                "unit_cost": "100", "purchase_order_line": order_line_id,
            }],
        }, format="json")
        self.assertEqual(receipt.status_code, 201, receipt.data)
        posted_receipt = self.client.post(
            f"/api/purchasing/receipts/{receipt.data['id']}/post/", {},
            format="json",
        )
        self.assertEqual(posted_receipt.status_code, 200, posted_receipt.data)

        invoice = self.client.post("/api/purchasing/invoices/", {
            "company": self.company.pk, "supplier": self.supplier_a.pk,
            "receipt": receipt.data["id"], "invoice_number": "INV-E2E-001",
            "invoice_date": self.invoice_date.isoformat(),
            "lines": [{
                "receipt_line": receipt.data["line_items"][0]["id"],
                "unit_price": "100", "tax_rate": "14",
            }],
        }, format="json")
        self.assertEqual(invoice.status_code, 201, invoice.data)
        posted_invoice = self.client.post(
            f"/api/purchasing/invoices/{invoice.data['id']}/post/", {},
            format="json",
        )
        self.assertEqual(posted_invoice.status_code, 200, posted_invoice.data)
        self.assertEqual(posted_invoice.data["grand_total"], "228.0000")

        payment = self.client.post("/api/purchasing/payments/", {
            "company": self.company.pk, "supplier": self.supplier_a.pk,
            "invoice": invoice.data["id"], "payment_number": "PAY-E2E-001",
            "payment_date": self.payment_date.isoformat(), "method": "bank",
            "payment_account": self.bank.pk, "amount": "228",
        }, format="json")
        self.assertEqual(payment.status_code, 201, payment.data)
        posted_payment = self.client.post(
            f"/api/purchasing/payments/{payment.data['id']}/post/", {},
            format="json",
        )
        self.assertEqual(posted_payment.status_code, 200, posted_payment.data)

        returned = self.client.post("/api/purchasing/returns/", {
            "company": self.company.pk, "supplier": self.supplier_a.pk,
            "invoice": invoice.data["id"], "return_number": "RET-E2E-001",
            "return_date": self.return_date.isoformat(),
            "lines": [{
                "invoice_line": invoice.data["invoice_lines"][0]["id"],
                "quantity": "1",
            }],
        }, format="json")
        self.assertEqual(returned.status_code, 201, returned.data)
        posted_return = self.client.post(
            f"/api/purchasing/returns/{returned.data['id']}/post/", {},
            format="json",
        )
        self.assertEqual(posted_return.status_code, 200, posted_return.data)
        self.assertEqual(posted_return.data["grand_total"], "114.0000")

        # Refund the supplier credit, reverse the refund, then reverse the
        # return in the required order to reconcile stock, AP, GL and treasury.
        refund_date = self.return_date + timedelta(days=1)
        refund = self.client.post("/api/purchasing/refunds/", {
            "company": self.company.pk, "supplier": self.supplier_a.pk,
            "return_document": posted_return.data["id"],
            "refund_number": "REF-E2E-001", "refund_date": refund_date.isoformat(),
            "method": "bank", "payment_account": self.bank.pk,
            "amount": "114", "reference": "BANK-E2E-REFUND",
        }, format="json")
        self.assertEqual(refund.status_code, 201, refund.data)
        posted_refund = self.client.post(
            f"/api/purchasing/refunds/{refund.data['id']}/post/", {},
            format="json",
        )
        self.assertEqual(posted_refund.status_code, 200, posted_refund.data)
        self.assertEqual(posted_refund.data["status"], "posted")

        refund_reversal_date = refund_date + timedelta(days=1)
        reversed_refund = self.client.post(
            f"/api/purchasing/refunds/{refund.data['id']}/reverse/",
            {"entry_date": refund_reversal_date.isoformat()}, format="json",
        )
        self.assertEqual(reversed_refund.status_code, 200, reversed_refund.data)
        self.assertEqual(reversed_refund.data["status"], "reversed")
        refund_movement = TreasuryTransaction.objects.get(
            journal_entry_id=posted_refund.data["journal_entry"],
        )
        # Treasury mirrors keep one source movement and link its reversal journal;
        # they do not create a second standalone cash movement for the reversal.
        self.assertEqual(refund_movement.status, TreasuryTransaction.Status.REVERSED)
        self.assertEqual(
            refund_movement.reversal_journal_entry_id,
            reversed_refund.data["reversal_journal_entry"],
        )
        self.assertEqual(refund_movement.kind, TreasuryTransaction.Kind.RECEIPT)

        return_reversal_date = refund_reversal_date + timedelta(days=1)
        reversed_return = self.client.post(
            f"/api/purchasing/returns/{posted_return.data['id']}/reverse/",
            {"entry_date": return_reversal_date.isoformat()}, format="json",
        )
        self.assertEqual(reversed_return.status_code, 200, reversed_return.data)
        self.assertEqual(reversed_return.data["status"], "reversed")

        balance = StockBalance.objects.get(
            product=self.product, warehouse=self.warehouse,
        )
        self.assertEqual(balance.quantity, Decimal("2.0000"))
        self.assertEqual(balance.inventory_value, Decimal("200.0000"))

        grni = self.client.get("/api/purchasing/receipts/grni-aging/", {
            "company": self.company.pk, "as_of": self.return_date.isoformat(),
        })
        self.assertEqual(grni.status_code, 200, grni.data)
        self.assertEqual(grni.data["grand_total"], "0.0000")
        self.assertEqual(grni.data["gl_reconciliation"]["difference"], "0.0000")
        self.assertTrue(grni.data["gl_reconciliation"]["reconciled"])

        invoice_after = self.client.get(
            f"/api/purchasing/invoices/{invoice.data['id']}/",
        )
        self.assertEqual(invoice_after.status_code, 200, invoice_after.data)
        self.assertEqual(invoice_after.data["paid_total"], "228.0000")
        self.assertEqual(invoice_after.data["balance_due"], "0.0000")

        statement = self.client.get("/api/purchasing/invoices/supplier-statement/", {
            "company": self.company.pk, "supplier": self.supplier_a.pk,
            "date_from": self.request_date.isoformat(),
            "date_to": return_reversal_date.isoformat(),
        })
        self.assertEqual(statement.status_code, 200, statement.data)
        self.assertEqual(statement.data["closing_balance"], "0.0000")

        aging = self.client.get("/api/purchasing/invoices/supplier-aging/", {
            "company": self.company.pk, "as_of": return_reversal_date.isoformat(),
        })
        self.assertEqual(aging.status_code, 200, aging.data)
        self.assertEqual(Decimal(aging.data["totals"]["total_due"]), Decimal("0"))

        reconciliation = self.client.get(
            f"/api/accounting/subledger-reconciliation/?company={self.company.pk}"
            f"&as_of={return_reversal_date.isoformat()}",
        )
        self.assertEqual(reconciliation.status_code, 200, reconciliation.data)
        supplier_row = next(row for row in reconciliation.data["rows"] if row["key"] == "suppliers")
        self.assertEqual(supplier_row["status"], "matched", supplier_row)
        self.assertEqual(Decimal(supplier_row["subledger_balance"]), Decimal("0"))
        self.assertEqual(Decimal(supplier_row["gl_balance"]), Decimal("0"))
        self.assertEqual(Decimal(supplier_row["difference"]), Decimal("0"))

        ap_account = AccountRole.objects.get(
            company=self.company, role=AccountRole.Role.ACCOUNTS_PAYABLE,
        ).account
        ap = JournalLine.objects.filter(
            account=ap_account, entry__company=self.company,
            entry__status__in=(JournalEntry.Status.POSTED, JournalEntry.Status.REVERSED),
        ).aggregate(debit=Sum("debit"), credit=Sum("credit"))
        self.assertEqual(ap["debit"], ap["credit"])
        self.assertEqual(ap["debit"] - ap["credit"], Decimal("0"))
        for entry in JournalEntry.objects.filter(
            company=self.company, status=JournalEntry.Status.POSTED,
        ):
            totals = entry.lines.aggregate(debit=Sum("debit"), credit=Sum("credit"))
            self.assertEqual(totals["debit"], totals["credit"], entry.entry_number)

        
        # Continue the same company journey across branches. The source branch
        # purchases and pays for the goods; a separately authorized destination
        # branch receives the transfer in two partial receipts and sells from its
        # own warehouse. This joins the previously isolated branch controls to the
        # purchasing, stock valuation, sales, treasury and GL journey.
        destination_branch = Branch.objects.create(
            company=self.company, code="REQE2E-DEST", name_ar="فرع الاستلام والبيع",
        )
        destination_warehouse = Warehouse.objects.create(
            company=self.company, branch=destination_branch,
            code="REQE2E-WH-DEST", name_ar="مخزن فرع الاستلام والبيع",
        )
        sender = get_user_model().objects.create_user(
            username="requisition-e2e-sender", password="test-password",
        )
        sender_membership = CompanyMembership.objects.create(
            user=sender, company=self.company, role="accountant", all_branches=False,
        )
        sender_membership.branches.add(self.branch)
        receiver = get_user_model().objects.create_user(
            username="requisition-e2e-receiver", password="test-password",
        )
        receiver_membership = CompanyMembership.objects.create(
            user=receiver, company=self.company, role="accountant", all_branches=False,
        )
        receiver_membership.branches.add(destination_branch)

        transfer_date = return_reversal_date + timedelta(days=1)
        self.client.force_authenticate(sender)
        transfer = self.client.post("/api/inventory/transfers/", {
            "company": self.company.pk,
            "transfer_number": "REQE2E-XFER-001",
            "transfer_date": transfer_date.isoformat(),
            "source_warehouse": self.warehouse.pk,
            "destination_warehouse": destination_warehouse.pk,
            "lines": [{"product": self.product.pk, "quantity": "2"}],
        }, format="json")
        self.assertEqual(transfer.status_code, 201, transfer.data)
        transfer_id = transfer.data["id"]
        transfer_line_id = transfer.data["lines"][0]["id"]
        dispatched = self.client.post(
            f"/api/inventory/transfers/{transfer_id}/dispatch/", {}, format="json",
        )
        self.assertEqual(dispatched.status_code, 200, dispatched.data)

        self.client.force_authenticate(receiver)
        receiver_cannot_dispatch = self.client.post(
            f"/api/inventory/transfers/{transfer_id}/dispatch/", {}, format="json",
        )
        self.assertEqual(receiver_cannot_dispatch.status_code, 403, receiver_cannot_dispatch.data)
        first_receipt_date = transfer_date + timedelta(days=1)
        first_receipt = self.client.post(
            f"/api/inventory/transfers/{transfer_id}/receive/",
            {
                "receipt_date": first_receipt_date.isoformat(),
                "lines": [{"transfer_line": transfer_line_id, "quantity": "1"}],
            },
            format="json",
        )
        self.assertEqual(first_receipt.status_code, 201, first_receipt.data)
        self.assertEqual(first_receipt.data["receipt_number"], f"TR-{transfer_id}-R1")
        transit_warehouse = Warehouse.objects.get(company=self.company, is_transit=True)
        transit_after_partial = StockBalance.objects.get(
            product=self.product, warehouse=transit_warehouse,
        )
        destination_after_partial = StockBalance.objects.get(
            product=self.product, warehouse=destination_warehouse,
        )
        self.assertEqual(transit_after_partial.quantity, Decimal("1.0000"))
        self.assertEqual(destination_after_partial.quantity, Decimal("1.0000"))

        self.client.force_authenticate(sender)
        sender_cannot_receive = self.client.post(
            f"/api/inventory/transfers/{transfer_id}/receive/",
            {
                "receipt_date": (first_receipt_date + timedelta(days=1)).isoformat(),
                "lines": [{"transfer_line": transfer_line_id, "quantity": "1"}],
            },
            format="json",
        )
        self.assertEqual(sender_cannot_receive.status_code, 403, sender_cannot_receive.data)

        self.client.force_authenticate(receiver)
        second_receipt_date = first_receipt_date + timedelta(days=1)
        second_receipt = self.client.post(
            f"/api/inventory/transfers/{transfer_id}/receive/",
            {
                "receipt_date": second_receipt_date.isoformat(),
                "lines": [{"transfer_line": transfer_line_id, "quantity": "1"}],
            },
            format="json",
        )
        self.assertEqual(second_receipt.status_code, 201, second_receipt.data)
        self.assertEqual(second_receipt.data["receipt_number"], f"TR-{transfer_id}-R2")
        transfer_detail = self.client.get(f"/api/inventory/transfers/{transfer_id}/")
        self.assertEqual(transfer_detail.status_code, 200, transfer_detail.data)
        self.assertEqual(transfer_detail.data["status"], "received")
        transit_after_full_receipt = StockBalance.objects.get(
            product=self.product, warehouse=transit_warehouse,
        )
        destination_after_full_receipt = StockBalance.objects.get(
            product=self.product, warehouse=destination_warehouse,
        )
        source_after_transfer = StockBalance.objects.get(
            product=self.product, warehouse=self.warehouse,
        )
        self.assertEqual(transit_after_full_receipt.quantity, Decimal("0.0000"))
        self.assertEqual(transit_after_full_receipt.inventory_value, Decimal("0.0000"))
        self.assertEqual(destination_after_full_receipt.quantity, Decimal("2.0000"))
        self.assertEqual(destination_after_full_receipt.inventory_value, Decimal("200.0000"))
        self.assertEqual(source_after_transfer.quantity, Decimal("0.0000"))
        self.assertEqual(source_after_transfer.inventory_value, Decimal("0.0000"))

        # A bank ledger is created for the destination branch so customer
        # collection is also tested against the receiving branch's own treasury.
        destination_bank_account = ChartOfAccount.objects.create(
            company=self.company, code="1101", name_ar="بنك فرع الاستلام",
            nature=ChartOfAccount.Nature.ASSET,
        )
        TreasuryAccount.objects.create(
            company=self.company, branch=destination_branch,
            account=destination_bank_account, kind=TreasuryAccount.Kind.BANK,
            code="BANK-REQE2E-DEST", name_ar="بنك فرع الاستلام",
            currency_code="EGP", opening_balance=Decimal("100000"),
        )

        sales_date = second_receipt_date + timedelta(days=1)
        delivery = self.client.post("/api/sales-documents/deliveries/", {
            "company": self.company.pk,
            "customer": self.customer.pk,
            "warehouse": destination_warehouse.pk,
            "delivery_number": "SALE-E2E-DEL-001",
            "delivery_date": sales_date.isoformat(),
            "lines": [{"product": self.product.pk, "quantity": "1"}],
        }, format="json")
        self.assertEqual(delivery.status_code, 201, delivery.data)
        posted_delivery = self.client.post(
            f"/api/sales-documents/deliveries/{delivery.data['id']}/post/",
            {}, format="json",
        )
        self.assertEqual(posted_delivery.status_code, 200, posted_delivery.data)

        sales_invoice_date = sales_date + timedelta(days=1)
        sales_invoice = self.client.post("/api/sales-documents/invoices/", {
            "company": self.company.pk,
            "customer": self.customer.pk,
            "delivery": delivery.data["id"],
            "invoice_number": "SALE-E2E-INV-001",
            "invoice_date": sales_invoice_date.isoformat(),
            "lines": [{
                "delivery_line": delivery.data["line_items"][0]["id"],
                "unit_price": "150",
                "tax_rate": "14",
            }],
        }, format="json")
        self.assertEqual(sales_invoice.status_code, 201, sales_invoice.data)
        posted_sales_invoice = self.client.post(
            f"/api/sales-documents/invoices/{sales_invoice.data['id']}/post/",
            {}, format="json",
        )
        self.assertEqual(posted_sales_invoice.status_code, 200, posted_sales_invoice.data)
        self.assertEqual(posted_sales_invoice.data["grand_total"], "171.0000")

        receipt_date = sales_invoice_date + timedelta(days=1)
        customer_receipt = self.client.post("/api/sales-documents/receipts/", {
            "company": self.company.pk,
            "customer": self.customer.pk,
            "invoice": sales_invoice.data["id"],
            "receipt_number": "SALE-E2E-RC-001",
            "receipt_date": receipt_date.isoformat(),
            "method": "bank",
            "payment_account": destination_bank_account.pk,
            "amount": "171",
        }, format="json")
        self.assertEqual(customer_receipt.status_code, 201, customer_receipt.data)
        posted_customer_receipt = self.client.post(
            f"/api/sales-documents/receipts/{customer_receipt.data['id']}/post/",
            {}, format="json",
        )
        self.assertEqual(posted_customer_receipt.status_code, 200, posted_customer_receipt.data)

        # Use the company-wide buyer membership for final cross-branch
        # reconciliation; the receiver remains branch-restricted for operations.
        self.client.force_authenticate(self.buyer)

        final_source_stock = StockBalance.objects.get(product=self.product, warehouse=self.warehouse)
        final_destination_stock = StockBalance.objects.get(
            product=self.product, warehouse=destination_warehouse,
        )
        self.assertEqual(final_source_stock.quantity, Decimal("0.0000"))
        self.assertEqual(final_source_stock.inventory_value, Decimal("0.0000"))
        self.assertEqual(final_destination_stock.quantity, Decimal("1.0000"))
        self.assertEqual(final_destination_stock.inventory_value, Decimal("100.0000"))

        inventory_reconciliation = self.client.get(
            f"/api/inventory/balances/reconciliation/?company={self.company.pk}",
        )
        self.assertEqual(
            inventory_reconciliation.status_code, 200, inventory_reconciliation.data,
        )
        self.assertEqual(inventory_reconciliation.data["stock_value"], "100.0000")
        self.assertEqual(inventory_reconciliation.data["gl_balance"], "100.0000")
        self.assertEqual(inventory_reconciliation.data["difference"], "0.0000")
        self.assertTrue(inventory_reconciliation.data["is_reconciled"])

        valuation_integrity = self.client.get(
            f"/api/inventory/balances/valuation-integrity/?company={self.company.pk}",
        )
        self.assertEqual(valuation_integrity.status_code, 200, valuation_integrity.data)
        self.assertEqual(valuation_integrity.data["fifo_mismatch_count"], 0)
        self.assertEqual(
            final_source_stock.inventory_value + final_destination_stock.inventory_value,
            Decimal("100.0000"),
        )

        clearing_account = AccountRole.objects.get(
            company=self.company, role=AccountRole.Role.INTER_BRANCH_CLEARING,
        ).account
        clearing_totals = JournalLine.objects.filter(
            entry__company=self.company, entry__status=JournalEntry.Status.POSTED,
            account=clearing_account,
        ).aggregate(debit=Sum("debit"), credit=Sum("credit"))
        self.assertEqual(clearing_totals["debit"], clearing_totals["credit"])

        final_date = receipt_date
        integrated_reconciliation = self.client.get(
            f"/api/accounting/subledger-reconciliation/?company={self.company.pk}"
            f"&as_of={final_date.isoformat()}",
        )
        self.assertEqual(integrated_reconciliation.status_code, 200, integrated_reconciliation.data)
        integrated_rows = {row["key"]: row for row in integrated_reconciliation.data["rows"]}
        self.assertEqual(integrated_rows["suppliers"]["status"], "matched", integrated_rows["suppliers"])
        self.assertEqual(integrated_rows["customers"]["status"], "matched", integrated_rows["customers"])
        self.assertEqual(Decimal(integrated_rows["suppliers"]["difference"]), Decimal("0"))
        self.assertEqual(Decimal(integrated_rows["customers"]["difference"]), Decimal("0"))

        trial_balance = self.client.get(
            f"/api/accounting/journal-entries/trial-balance/?company={self.company.pk}"
            f"&date_from={self.request_date.isoformat()}&date_to={final_date.isoformat()}",
        )
        self.assertEqual(trial_balance.status_code, 200, trial_balance.data)
        self.assertEqual(trial_balance.data["difference"], Decimal("0.0000"))
        self.assertEqual(trial_balance.data["debit_total"], trial_balance.data["credit_total"])

        income_statement = self.client.get(
            f"/api/accounting/journal-entries/income-statement/?company={self.company.pk}"
            f"&date_from={self.request_date.isoformat()}&date_to={final_date.isoformat()}",
        )
        self.assertEqual(income_statement.status_code, 200, income_statement.data)
        self.assertEqual(income_statement.data["revenue_total"], Decimal("150.0000"))
        self.assertEqual(income_statement.data["cost_of_sales_total"], Decimal("100.0000"))
        self.assertEqual(income_statement.data["gross_profit"], Decimal("50.0000"))

        # Close the customer side of the same cross-branch journey with a
        # posted sales return and its reversal. This verifies that stock,
        # customer balance, tax, COGS and the GL all return to their pre-return
        # state instead of validating those modules only in isolated tests.
        sales_return_date = final_date + timedelta(days=1)
        sales_return = self.client.post("/api/sales-documents/returns/", {
            "company": self.company.pk,
            "invoice": sales_invoice.data["id"],
            "return_number": "SALE-E2E-RET-001",
            "return_date": sales_return_date.isoformat(),
            "lines": [{
                "invoice_line": sales_invoice.data["invoice_lines"][0]["id"],
                "quantity": "1",
            }],
        }, format="json")
        self.assertEqual(sales_return.status_code, 201, sales_return.data)
        posted_sales_return = self.client.post(
            f"/api/sales-documents/returns/{sales_return.data['id']}/post/",
            {}, format="json",
        )
        self.assertEqual(posted_sales_return.status_code, 200, posted_sales_return.data)
        self.assertEqual(posted_sales_return.data["status"], "posted")
        self.assertEqual(posted_sales_return.data["grand_total"], "171.0000")

        sales_return_reversal_date = sales_return_date + timedelta(days=1)
        reversed_sales_return = self.client.post(
            f"/api/sales-documents/returns/{sales_return.data['id']}/reverse/",
            {"entry_date": sales_return_reversal_date.isoformat()},
            format="json",
        )
        self.assertEqual(reversed_sales_return.status_code, 200, reversed_sales_return.data)
        self.assertEqual(reversed_sales_return.data["status"], "reversed")
        self.assertIsNotNone(reversed_sales_return.data["reversal_journal_entry"])
        self.assertIsNotNone(reversed_sales_return.data["reversal_stock_movement"])

        final_destination_stock.refresh_from_db()
        self.assertEqual(final_destination_stock.quantity, Decimal("1.0000"))
        self.assertEqual(final_destination_stock.inventory_value, Decimal("100.0000"))
        sales_invoice_after_return_reversal = self.client.get(
            f"/api/sales-documents/invoices/{sales_invoice.data['id']}/",
        )
        self.assertEqual(
            sales_invoice_after_return_reversal.status_code, 200,
            sales_invoice_after_return_reversal.data,
        )
        self.assertEqual(sales_invoice_after_return_reversal.data["paid_total"], "171.0000")
        self.assertEqual(sales_invoice_after_return_reversal.data["balance_due"], "0.0000")

        final_reconciliation = self.client.get(
            f"/api/accounting/subledger-reconciliation/?company={self.company.pk}"
            f"&as_of={sales_return_reversal_date.isoformat()}",
        )
        self.assertEqual(final_reconciliation.status_code, 200, final_reconciliation.data)
        final_rows = {row["key"]: row for row in final_reconciliation.data["rows"]}
        for key in ("suppliers", "customers"):
            self.assertEqual(final_rows[key]["status"], "matched", final_rows[key])
            self.assertEqual(Decimal(final_rows[key]["difference"]), Decimal("0"))

        final_trial_balance = self.client.get(
            f"/api/accounting/journal-entries/trial-balance/?company={self.company.pk}"
            f"&date_from={self.request_date.isoformat()}"
            f"&date_to={sales_return_reversal_date.isoformat()}",
        )
        self.assertEqual(final_trial_balance.status_code, 200, final_trial_balance.data)
        self.assertEqual(final_trial_balance.data["difference"], Decimal("0.0000"))
        self.assertEqual(
            final_trial_balance.data["debit_total"],
            final_trial_balance.data["credit_total"],
        )

        final_income_statement = self.client.get(
            f"/api/accounting/journal-entries/income-statement/?company={self.company.pk}"
            f"&date_from={self.request_date.isoformat()}"
            f"&date_to={sales_return_reversal_date.isoformat()}",
        )
        self.assertEqual(final_income_statement.status_code, 200, final_income_statement.data)
        self.assertEqual(final_income_statement.data["revenue_total"], Decimal("150.0000"))
        self.assertEqual(final_income_statement.data["cost_of_sales_total"], Decimal("100.0000"))
        self.assertEqual(final_income_statement.data["gross_profit"], Decimal("50.0000"))
