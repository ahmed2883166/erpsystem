from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from accounting.models import AccountRole, ChartOfAccount, JournalEntry, JournalLine
from companies.models import Branch, Company, CompanyMembership, Currency, FiscalYear
from inventory.models import Product, StockBalance, UnitOfMeasure, Warehouse
from suppliers.models import Supplier


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
            AccountRole.Role.GOODS_RECEIVED_NOT_INVOICED: ("2100", "بضاعة مستلمة غير مفوترة", ChartOfAccount.Nature.LIABILITY),
            AccountRole.Role.ACCOUNTS_PAYABLE: ("2200", "الموردون", ChartOfAccount.Nature.LIABILITY),
            AccountRole.Role.VAT_INPUT: ("1300", "ضريبة مدخلات", ChartOfAccount.Nature.ASSET),
            AccountRole.Role.BANK: ("1100", "البنك", ChartOfAccount.Nature.ASSET),
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
        purchase_lines = list(JournalLine.objects.filter(entry__source_type=JournalEntry.Source.PURCHASES))
        self.assertEqual(sum((line.debit for line in purchase_lines), Decimal("0")), Decimal("228.0000"))
        self.assertEqual(sum((line.credit for line in purchase_lines), Decimal("0")), Decimal("228.0000"))

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

        payment_response = self.client.post("/api/purchasing/payments/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "invoice": invoice_id,
            "payment_number": "PAY-001", "payment_date": "2026-03-15",
            "method": "bank", "amount": "100.0000", "reference": "BANK-REF-1",
        }, format="json")
        self.assertEqual(payment_response.status_code, 201, payment_response.data)
        payment_id = payment_response.data["id"]
        posted_payment = self.client.post(
            f"/api/purchasing/payments/{payment_id}/post/", {}, format="json"
        )
        self.assertEqual(posted_payment.status_code, 200, posted_payment.data)
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

        aging = self.client.get(
            "/api/purchasing/invoices/supplier-aging/",
            {"company": self.company.pk, "as_of": "2026-03-25"},
        )
        self.assertEqual(aging.status_code, 200, aging.data)
        self.assertEqual(aging.data["totals"]["1_30"], "178.0000")
        self.assertEqual(aging.data["totals"]["total_due"], "178.0000")
        self.assertEqual(aging.data["suppliers"][0]["invoices"][0]["overdue_days"], 15)

    def test_invoice_rejects_price_mismatch_against_grni(self):
        receipt_id, receipt_line_id = self._create_and_post_receipt()
        response = self.client.post("/api/purchasing/invoices/", {
            "company": self.company.pk, "supplier": self.supplier.pk, "receipt": receipt_id,
            "invoice_number": "INV-S-002", "invoice_date": "2026-03-10",
            "lines": [{"receipt_line": receipt_line_id, "unit_price": "95", "tax_rate": "14"}],
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        response = self.client.post(f"/api/purchasing/invoices/{response.data['id']}/post/", {}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(StockBalance.objects.get(product=self.product, warehouse=self.warehouse).quantity, Decimal("2"))
        self.assertFalse(JournalEntry.objects.filter(source_type=JournalEntry.Source.PURCHASES).exists())

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
