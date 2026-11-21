from datetime import date

from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from accounting.models import AccountRole, ChartOfAccount, JournalEntry, JournalLine
from companies.models import Branch, Company, CompanyMembership, Currency, FiscalYear
from .models import Product, ProductCategory, StockBalance, StockCostLayer, StockMovement, StockMovementJournalLink, UnitOfMeasure, Warehouse


class InventoryApiTests(APITestCase):
    def setUp(self):
        currency = Currency.objects.create(code="EGP", name_ar="جنيه مصري")
        self.company = Company.objects.create(code="INV", name_ar="شركة المخزون", country="EG",
                                              activity="commercial", base_currency=currency)
        self.other_company = Company.objects.create(code="INV2", name_ar="شركة أخرى", country="SA",
                                                    activity="commercial", base_currency=currency)
        self.branch = Branch.objects.create(company=self.company, code="B1", name_ar="الفرع الرئيسي")
        self.user = get_user_model().objects.create_user(username="inventory_admin", password="test-password")
        CompanyMembership.objects.create(user=self.user, company=self.company, role="admin", all_branches=True)
        self.client.force_authenticate(self.user)
        self.unit = UnitOfMeasure.objects.create(company=self.company, code="PCS", name_ar="قطعة")
        self.category = ProductCategory.objects.create(company=self.company, code="CAT", name_ar="عام")
        self.fiscal_year = FiscalYear.objects.create(
            company=self.company, name="2026", start_date=date(2026, 1, 1), end_date=date(2026, 12, 31)
        )
        role_accounts = (
            (AccountRole.Role.INVENTORY, "1000", "مخزون", ChartOfAccount.Nature.ASSET),
            (AccountRole.Role.GOODS_RECEIVED_NOT_INVOICED, "2100", "بضاعة مستلمة غير مفوترة", ChartOfAccount.Nature.LIABILITY),
            (AccountRole.Role.COGS, "5000", "تكلفة المبيعات", ChartOfAccount.Nature.EXPENSE),
            (AccountRole.Role.INVENTORY_ADJUSTMENT_GAIN, "4900", "أرباح تسوية مخزون", ChartOfAccount.Nature.REVENUE),
            (AccountRole.Role.INVENTORY_ADJUSTMENT_LOSS, "5900", "خسائر تسوية مخزون", ChartOfAccount.Nature.EXPENSE),
            (AccountRole.Role.INTER_BRANCH_CLEARING, "2300", "تسوية بين الفروع", ChartOfAccount.Nature.ASSET),
        )
        for role, code, name, nature in role_accounts:
            account = ChartOfAccount.objects.create(
                company=self.company, code=code, name_ar=name, nature=nature, allow_posting=True
            )
            AccountRole.objects.create(company=self.company, role=role, account=account)

    def test_product_create_and_company_scope(self):
        hidden_unit = UnitOfMeasure.objects.create(company=self.other_company, code="PCS", name_ar="قطعة")
        hidden = Product.objects.create(company=self.other_company, unit=hidden_unit, code="P1", name_ar="صنف مخفي")
        response = self.client.get("/api/inventory/products/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 0) if isinstance(response.data, dict) and "count" in response.data else self.assertEqual(len(response.data), 0)
        created = self.client.post("/api/inventory/products/", {
            "company": self.company.pk, "unit": self.unit.pk, "category": self.category.pk,
            "code": " p-001 ", "barcode": "123456", "name_ar": "صنف تجريبي",
            "purchase_price": "10.2500", "sale_price": "15.0000",
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        self.assertEqual(created.data["code"], "P-001")
        self.assertNotEqual(hidden.pk, created.data["id"])

    def test_product_rejects_foreign_company_unit(self):
        foreign_unit = UnitOfMeasure.objects.create(company=self.other_company, code="BOX", name_ar="كرتونة")
        response = self.client.post("/api/inventory/products/", {
            "company": self.company.pk, "unit": foreign_unit.pk, "code": "P-002", "name_ar": "غير صحيح"
        }, format="json")
        self.assertEqual(response.status_code, 400)

    def test_barcode_unique_within_company(self):
        Product.objects.create(company=self.company, unit=self.unit, code="P-003", barcode="9988", name_ar="أول")
        response = self.client.post("/api/inventory/products/", {
            "company": self.company.pk, "unit": self.unit.pk, "code": "P-004",
            "barcode": "9988", "name_ar": "ثاني",
        }, format="json")
        self.assertEqual(response.status_code, 400)

    def test_warehouse_branch_must_belong_to_company(self):
        foreign_branch = Branch.objects.create(company=self.other_company, code="B2", name_ar="فرع آخر")
        response = self.client.post("/api/inventory/warehouses/", {
            "company": self.company.pk, "branch": foreign_branch.pk,
            "code": "WH1", "name_ar": "مخزن غير صحيح",
        }, format="json")
        self.assertEqual(response.status_code, 400)

    def test_storage_location_create_and_soft_delete(self):
        warehouse = Warehouse.objects.create(company=self.company, branch=self.branch, code="WH-01", name_ar="المخزن")
        created = self.client.post("/api/inventory/locations/", {
            "warehouse": warehouse.pk, "code": "F1", "name_ar": "الدور الأول",
            "location_type": "floor",
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        deleted = self.client.delete(f"/api/inventory/locations/{created.data['id']}/")
        self.assertEqual(deleted.status_code, 204)
        from .models import StorageLocation
        location = StorageLocation.objects.get(pk=created.data["id"])
        self.assertFalse(location.is_active)

    def test_viewer_cannot_create_storage_location(self):
        warehouse = Warehouse.objects.create(company=self.company, branch=self.branch, code="WH-02", name_ar="مخزن")
        viewer = get_user_model().objects.create_user(username="location_viewer", password="test-password")
        CompanyMembership.objects.create(user=viewer, company=self.company, role="viewer", all_branches=True)
        self.client.force_authenticate(viewer)
        response = self.client.post("/api/inventory/locations/", {
            "warehouse": warehouse.pk, "code": "A1", "name_ar": "منطقة",
        }, format="json")
        self.assertEqual(response.status_code, 403, response.data)

    def test_viewer_cannot_create_inventory_product(self):
        viewer = get_user_model().objects.create_user(username="inventory_viewer", password="test-password")
        CompanyMembership.objects.create(user=viewer, company=self.company, role="viewer", all_branches=True)
        self.client.force_authenticate(viewer)
        response = self.client.post("/api/inventory/products/", {
            "company": self.company.pk, "unit": self.unit.pk, "category": self.category.pk,
            "code": "P-005", "name_ar": "ممنوع", "product_type": "goods",
            "purchase_price": "0", "sale_price": "0", "min_stock": "0"
        }, format="json")
        self.assertEqual(response.status_code, 403, response.data)


    def _warehouse(self, code):
        return Warehouse.objects.create(company=self.company, branch=self.branch, code=code, name_ar=code)

    def _movement(self, number, movement_type, destination=None, source=None, quantity="5", cost="10", source_location=None, destination_location=None, expiry_date=None, adjustment_direction=""):
        return self.client.post("/api/inventory/movements/", {
            "company": self.company.pk,
            "movement_number": number,
            "movement_type": movement_type,
            "movement_date": "2026-11-21",
            "source_warehouse": source.pk if source else None,
            "destination_warehouse": destination.pk if destination else None,
            "lines": [{
                "product": self.product.pk,
                "quantity": quantity,
                "unit_cost": cost,
                "source_location": source_location.pk if source_location else None,
                "destination_location": destination_location.pk if destination_location else None,
                "expiry_date": expiry_date,
                "adjustment_direction": adjustment_direction,
            }],
        }, format="json")

    def test_receipt_posts_and_updates_weighted_average_balance(self):
        self.product = Product.objects.create(company=self.company, unit=self.unit, code="STK-1", name_ar="صنف رصيد")
        warehouse = self._warehouse("ST-01")
        created = self._movement("RCV-001", "receipt", destination=warehouse, quantity="4", cost="12.5")
        self.assertEqual(created.status_code, 201, created.data)
        posted = self.client.post(f"/api/inventory/movements/{created.data['id']}/post/", {}, format="json")
        self.assertEqual(posted.status_code, 200, posted.data)
        balance = StockBalance.objects.get(product=self.product, warehouse=warehouse)
        self.assertEqual(str(balance.quantity), "4.0000")
        self.assertEqual(str(balance.inventory_value), "50.0000")
        self.assertEqual(balance.average_cost, 12.5)
        self.assertEqual(posted.data["status"], StockMovement.Status.POSTED)

    def test_issue_cannot_make_stock_negative_and_is_atomic(self):
        self.product = Product.objects.create(company=self.company, unit=self.unit, code="STK-2", name_ar="صنف صرف")
        warehouse = self._warehouse("ST-02")
        created = self._movement("ISS-001", "issue", source=warehouse, quantity="1")
        self.assertEqual(created.status_code, 201, created.data)
        response = self.client.post(f"/api/inventory/movements/{created.data['id']}/post/", {}, format="json")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(StockBalance.objects.filter(product=self.product, warehouse=warehouse).exists())
        self.assertEqual(StockMovement.objects.get(pk=created.data["id"]).status, StockMovement.Status.DRAFT)

    def test_transfer_moves_quantity_and_value_between_warehouses(self):
        self.product = Product.objects.create(company=self.company, unit=self.unit, code="STK-3", name_ar="صنف تحويل")
        source = self._warehouse("ST-03")
        destination = self._warehouse("ST-04")
        receipt = self._movement("RCV-003", "receipt", destination=source, quantity="10", cost="7")
        self.assertEqual(receipt.status_code, 201, receipt.data)
        self.assertEqual(self.client.post(f"/api/inventory/movements/{receipt.data['id']}/post/", {}, format="json").status_code, 200)
        transfer = self._movement("TRF-001", "transfer", destination=destination, source=source, quantity="3")
        self.assertEqual(transfer.status_code, 201, transfer.data)
        posted = self.client.post(f"/api/inventory/movements/{transfer.data['id']}/post/", {}, format="json")
        self.assertEqual(posted.status_code, 200, posted.data)
        source_balance = StockBalance.objects.get(product=self.product, warehouse=source)
        destination_balance = StockBalance.objects.get(product=self.product, warehouse=destination)
        self.assertEqual(str(source_balance.quantity), "7.0000")
        self.assertEqual(str(destination_balance.quantity), "3.0000")
        self.assertEqual(str(destination_balance.inventory_value), "21.0000")

    def test_posted_movement_cannot_be_posted_twice(self):
        self.product = Product.objects.create(company=self.company, unit=self.unit, code="STK-4", name_ar="صنف تكرار")
        warehouse = self._warehouse("ST-05")
        created = self._movement("RCV-004", "receipt", destination=warehouse, quantity="2", cost="3")
        self.assertEqual(created.status_code, 201, created.data)
        first = self.client.post(f"/api/inventory/movements/{created.data['id']}/post/", {}, format="json")
        second = self.client.post(f"/api/inventory/movements/{created.data['id']}/post/", {}, format="json")
        self.assertEqual(first.status_code, 200, first.data)
        self.assertEqual(second.status_code, 400, second.data)
        balance = StockBalance.objects.get(product=self.product, warehouse=warehouse)
        self.assertEqual(str(balance.quantity), "2.0000")


    def test_fifo_consumes_oldest_cost_layer_first(self):
        self.product = Product.objects.create(
            company=self.company, unit=self.unit, code="FIFO-1", name_ar="صنف FIFO", costing_method="fifo"
        )
        warehouse = self._warehouse("FIFO-WH")
        first = self._movement("FIFO-R1", "receipt", destination=warehouse, quantity="3", cost="5")
        second = self._movement("FIFO-R2", "receipt", destination=warehouse, quantity="3", cost="10")
        self.assertEqual(first.status_code, 201, first.data)
        self.assertEqual(second.status_code, 201, second.data)
        self.assertEqual(self.client.post(f"/api/inventory/movements/{first.data['id']}/post/", {}, format="json").status_code, 200)
        self.assertEqual(self.client.post(f"/api/inventory/movements/{second.data['id']}/post/", {}, format="json").status_code, 200)
        issue = self._movement("FIFO-I1", "issue", source=warehouse, quantity="4")
        self.assertEqual(issue.status_code, 201, issue.data)
        posted = self.client.post(f"/api/inventory/movements/{issue.data['id']}/post/", {}, format="json")
        self.assertEqual(posted.status_code, 200, posted.data)
        balance = StockBalance.objects.get(product=self.product, warehouse=warehouse)
        self.assertEqual(str(balance.quantity), "2.0000")
        self.assertEqual(str(balance.inventory_value), "20.0000")
        self.assertEqual(StockCostLayer.objects.filter(product=self.product, warehouse=warehouse, quantity_remaining__gt=0).count(), 1)
        self.assertEqual(str(StockCostLayer.objects.get(product=self.product, warehouse=warehouse, quantity_remaining__gt=0).unit_cost), "10.000000")


    def test_admin_can_delete_only_a_draft_movement(self):
        self.product = Product.objects.create(company=self.company, unit=self.unit, code="DEL-1", name_ar="مسودة")
        warehouse = self._warehouse("DEL-WH")
        created = self._movement("DEL-001", "receipt", destination=warehouse)
        self.assertEqual(created.status_code, 201, created.data)
        response = self.client.delete(f"/api/inventory/movements/{created.data['id']}/")
        self.assertEqual(response.status_code, 204)
        self.assertFalse(StockMovement.objects.filter(pk=created.data["id"]).exists())


    def test_costing_method_cannot_change_with_existing_stock(self):
        self.product = Product.objects.create(company=self.company, unit=self.unit, code="LOCK-COST", name_ar="تثبيت التقييم")
        warehouse = self._warehouse("LOCK-WH")
        receipt = self._movement("LOCK-R1", "receipt", destination=warehouse, quantity="2", cost="8")
        self.assertEqual(receipt.status_code, 201, receipt.data)
        self.assertEqual(self.client.post(f"/api/inventory/movements/{receipt.data['id']}/post/", {}, format="json").status_code, 200)
        response = self.client.patch(
            f"/api/inventory/products/{self.product.pk}/",
            {"costing_method": "fifo"},
            format="json",
        )
        self.assertEqual(response.status_code, 400, response.data)
        self.product.refresh_from_db()
        self.assertEqual(self.product.costing_method, Product.CostingMethod.WEIGHTED_AVERAGE)


    def test_expiry_tracked_receipt_updates_location_specific_balance(self):
        from datetime import date
        from .models import StorageLocation
        self.product = Product.objects.create(
            company=self.company, unit=self.unit, code="EXP-1", name_ar="صنف صلاحية", track_expiry=True
        )
        warehouse = self._warehouse("EXP-WH")
        location = StorageLocation.objects.create(
            warehouse=warehouse, code="RACK-1", name_ar="رف 1", location_type="rack"
        )
        created = self._movement(
            "EXP-R1", "receipt", destination=warehouse, quantity="6", cost="4",
            destination_location=location, expiry_date="2027-05-01",
        )
        self.assertEqual(created.status_code, 201, created.data)
        posted = self.client.post(f"/api/inventory/movements/{created.data['id']}/post/", {}, format="json")
        self.assertEqual(posted.status_code, 200, posted.data)
        balance = StockBalance.objects.get(
            product=self.product, warehouse=warehouse, location=location, expiry_date=date(2027, 5, 1)
        )
        self.assertEqual(str(balance.quantity), "6.0000")

    def test_expiry_tracked_product_requires_expiry_date(self):
        self.product = Product.objects.create(
            company=self.company, unit=self.unit, code="EXP-2", name_ar="صنف صلاحية 2", track_expiry=True
        )
        warehouse = self._warehouse("EXP-WH2")
        response = self._movement("EXP-R2", "receipt", destination=warehouse)
        self.assertEqual(response.status_code, 400, response.data)


    def test_receipt_posts_balanced_inventory_journal(self):
        self.product = Product.objects.create(company=self.company, unit=self.unit, code="GL-R1", name_ar="استلام محاسبي")
        warehouse = self._warehouse("GL-WH1")
        created = self._movement("GL-RCV1", "receipt", destination=warehouse, quantity="4", cost="12.5")
        self.assertEqual(created.status_code, 201, created.data)
        response = self.client.post(f"/api/inventory/movements/{created.data['id']}/post/", {}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        link = StockMovementJournalLink.objects.get(movement_id=created.data["id"])
        entry = link.journal_entry
        self.assertEqual(entry.status, JournalEntry.Status.POSTED)
        self.assertEqual(entry.source_type, JournalEntry.Source.INVENTORY)
        self.assertEqual(entry.fiscal_year_id, self.fiscal_year.pk)
        lines = list(entry.lines.select_related("account"))
        self.assertEqual(sum((line.debit for line in lines), 0), 50)
        self.assertEqual(sum((line.credit for line in lines), 0), 50)
        self.assertEqual({line.account.code for line in lines}, {"1000", "2100"})

    def test_cross_branch_transfer_creates_two_balanced_branch_entries(self):
        self.product = Product.objects.create(company=self.company, unit=self.unit, code="GL-T1", name_ar="تحويل فروع")
        source = self._warehouse("GL-SRC")
        other_branch = Branch.objects.create(company=self.company, code="B2", name_ar="الفرع الثاني")
        destination = Warehouse.objects.create(company=self.company, branch=other_branch, code="GL-DST", name_ar="مخزن الفرع الثاني")
        receipt = self._movement("GL-RCV2", "receipt", destination=source, quantity="5", cost="8")
        self.assertEqual(receipt.status_code, 201, receipt.data)
        self.assertEqual(self.client.post(f"/api/inventory/movements/{receipt.data['id']}/post/", {}, format="json").status_code, 200)
        transfer = self._movement("GL-TRF1", "transfer", destination=destination, source=source, quantity="2")
        self.assertEqual(transfer.status_code, 201, transfer.data)
        response = self.client.post(f"/api/inventory/movements/{transfer.data['id']}/post/", {}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        links = StockMovementJournalLink.objects.filter(movement_id=transfer.data["id"]).select_related("journal_entry")
        self.assertEqual(links.count(), 2)
        self.assertEqual({link.journal_entry.branch_id for link in links}, {self.branch.pk, other_branch.pk})
        for link in links:
            lines = list(link.journal_entry.lines.select_related("account"))
            self.assertEqual(sum((line.debit for line in lines), 0), 16)
            self.assertEqual(sum((line.credit for line in lines), 0), 16)
            self.assertIn("2300", {line.account.code for line in lines})
