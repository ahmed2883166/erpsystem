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
        self.assertEqual(aging.data["totals"]["total_due"], "0.0000")

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
