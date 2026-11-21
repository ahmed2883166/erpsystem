from datetime import date

from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from accounting.models import AccountRole, ChartOfAccount, JournalEntry, JournalLine
from companies.models import Branch, Company, CompanyMembership, Currency, FiscalYear
from .models import Product, ProductCategory, StockBalance, StockCostLayer, StockMovement, StockMovementJournalLink, UnitOfMeasure, Warehouse, WarehouseProductSetting


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

    def test_unassigned_warehouse_is_scoped_to_the_company_default_branch(self):
        restricted_branch = Branch.objects.create(
            company=self.company, code="B2", name_ar="فرع محدود",
        )
        shared = Warehouse.objects.create(
            company=self.company, branch=None, code="WH-SHARED", name_ar="مخزن عام",
        )
        product = Product.objects.create(
            company=self.company, unit=self.unit, code="WH-SCOPE-PROD", name_ar="صنف اختبار",
        )
        limited = get_user_model().objects.create_user(username="warehouse-branch-limited", password="test-password")
        membership = CompanyMembership.objects.create(
            user=limited, company=self.company, role="accountant", all_branches=False,
        )
        membership.branches.add(restricted_branch)
        self.client.force_authenticate(limited)

        listed = self.client.get("/api/inventory/warehouses/")
        self.assertEqual(listed.status_code, 200)
        ids = [row["id"] for row in listed.data] if isinstance(listed.data, list) else [row["id"] for row in listed.data["results"]]
        self.assertNotIn(shared.pk, ids)

        movement = self.client.post("/api/inventory/movements/", {
            "company": self.company.pk, "movement_type": "receipt", "movement_date": "2026-03-10",
            "destination_warehouse": shared.pk, "movement_number": "WH-SCOPE-001",
            "lines": [{"product": product.pk, "quantity": "1", "unit_cost": "10"}],
        }, format="json")
        self.assertEqual(movement.status_code, 403, movement.data)

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


    def test_stock_card_returns_opening_period_and_running_balances(self):
        self.product = Product.objects.create(company=self.company, unit=self.unit, code="CARD-1", name_ar="بطاقة صنف")
        warehouse = self._warehouse("CARD-WH")
        receipt = self._movement("CARD-RCV", "receipt", destination=warehouse, quantity="4", cost="10")
        self.assertEqual(receipt.status_code, 201, receipt.data)
        self.assertEqual(self.client.post(f"/api/inventory/movements/{receipt.data['id']}/post/", {}, format="json").status_code, 200)
        issue = self._movement("CARD-ISS", "issue", source=warehouse, quantity="1")
        self.assertEqual(issue.status_code, 201, issue.data)
        issue_record = StockMovement.objects.get(pk=issue.data["id"])
        issue_record.movement_date = date(2026, 11, 23)
        issue_record.save(update_fields=("movement_date",))
        self.assertEqual(self.client.post(f"/api/inventory/movements/{issue.data['id']}/post/", {}, format="json").status_code, 200)

        response = self.client.get(
            f"/api/inventory/movements/stock-card/?product={self.product.pk}&warehouse={warehouse.pk}&date_from=2026-11-22&date_to=2026-11-30"
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["opening_quantity"], "4.0000")
        self.assertEqual(response.data["opening_value"], "40.0000")
        self.assertEqual(len(response.data["rows"]), 1)
        self.assertEqual(response.data["rows"][0]["quantity_out"], "1.0000")
        self.assertEqual(response.data["closing_quantity"], "3.0000")
        self.assertEqual(response.data["closing_value"], "30.0000")

    def test_warehouse_product_reorder_setting_is_scoped_and_persisted(self):
        product = Product.objects.create(company=self.company, unit=self.unit, code="MIN-1", name_ar="حد مخزن")
        warehouse = self._warehouse("MIN-WH")
        created = self.client.post("/api/inventory/warehouse-product-settings/", {
            "product": product.pk, "warehouse": warehouse.pk, "min_stock": "7.5000",
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        setting = WarehouseProductSetting.objects.get(product=product, warehouse=warehouse)
        self.assertEqual(setting.min_stock, 7.5)
        listed = self.client.get("/api/inventory/warehouse-product-settings/")
        self.assertEqual(listed.status_code, 200, listed.data)
        self.assertEqual(len(listed.data), 1)
        updated = self.client.patch(f"/api/inventory/warehouse-product-settings/{setting.pk}/", {
            "min_stock": "12.0000",
        }, format="json")
        self.assertEqual(updated.status_code, 200, updated.data)
        setting.refresh_from_db()
        self.assertEqual(setting.min_stock, 12)

    def test_warehouse_product_reorder_setting_rejects_cross_company_product(self):
        foreign_unit = UnitOfMeasure.objects.create(company=self.other_company, code="PCS", name_ar="قطعة")
        foreign_product = Product.objects.create(company=self.other_company, unit=foreign_unit, code="FOREIGN-MIN", name_ar="صنف شركة أخرى")
        warehouse = self._warehouse("MIN-WH2")
        response = self.client.post("/api/inventory/warehouse-product-settings/", {
            "product": foreign_product.pk, "warehouse": warehouse.pk, "min_stock": "3",
        }, format="json")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(WarehouseProductSetting.objects.filter(product=foreign_product, warehouse=warehouse).exists())

    def test_multi_line_receipt_posts_all_products_and_balanced_total(self):
        first = Product.objects.create(company=self.company, unit=self.unit, code="ML-1", name_ar="صنف متعدد 1")
        second = Product.objects.create(company=self.company, unit=self.unit, code="ML-2", name_ar="صنف متعدد 2")
        warehouse = self._warehouse("ML-WH")
        created = self.client.post("/api/inventory/movements/", {
            "company": self.company.pk,
            "movement_number": "ML-RCV-1",
            "movement_type": "receipt",
            "movement_date": "2026-11-21",
            "destination_warehouse": warehouse.pk,
            "lines": [
                {"product": first.pk, "quantity": "2", "unit_cost": "10"},
                {"product": second.pk, "quantity": "3", "unit_cost": "5"},
            ],
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        posted = self.client.post(f"/api/inventory/movements/{created.data['id']}/post/", {}, format="json")
        self.assertEqual(posted.status_code, 200, posted.data)
        self.assertEqual(StockBalance.objects.get(product=first, warehouse=warehouse).quantity, 2)
        self.assertEqual(StockBalance.objects.get(product=second, warehouse=warehouse).quantity, 3)
        link = StockMovementJournalLink.objects.get(movement_id=created.data["id"])
        lines = list(link.journal_entry.lines.select_related("account"))
        self.assertEqual(sum((line.debit for line in lines), 0), 35)
        self.assertEqual(sum((line.credit for line in lines), 0), 35)

    def test_fifo_uses_movement_date_for_backdated_receipts(self):
        self.product = Product.objects.create(
            company=self.company, unit=self.unit, code="FIFO-DATE", name_ar="FIFO بتاريخ الحركة", costing_method="fifo"
        )
        warehouse = self._warehouse("FIFO-DATE-WH")

        later = self._movement("FIFO-DATE-LATE", "receipt", destination=warehouse, quantity="3", cost="20")
        self.assertEqual(later.status_code, 201, later.data)
        later_record = StockMovement.objects.get(pk=later.data["id"])
        later_record.movement_date = date(2026, 11, 25)
        later_record.save(update_fields=("movement_date",))
        self.assertEqual(self.client.post(f"/api/inventory/movements/{later.data['id']}/post/", {}, format="json").status_code, 200)

        earlier = self._movement("FIFO-DATE-EARLY", "receipt", destination=warehouse, quantity="3", cost="10")
        self.assertEqual(earlier.status_code, 201, earlier.data)
        earlier_record = StockMovement.objects.get(pk=earlier.data["id"])
        earlier_record.movement_date = date(2026, 11, 20)
        earlier_record.save(update_fields=("movement_date",))
        self.assertEqual(self.client.post(f"/api/inventory/movements/{earlier.data['id']}/post/", {}, format="json").status_code, 200)

        issue = self._movement("FIFO-DATE-ISSUE", "issue", source=warehouse, quantity="1")
        self.assertEqual(issue.status_code, 201, issue.data)
        posted = self.client.post(f"/api/inventory/movements/{issue.data['id']}/post/", {}, format="json")
        self.assertEqual(posted.status_code, 200, posted.data)

        issue_line = StockMovement.objects.get(pk=issue.data["id"]).lines.get()
        self.assertEqual(issue_line.cost_amount, 10)
        remaining_layers = list(
            StockCostLayer.objects.filter(product=self.product, warehouse=warehouse, quantity_remaining__gt=0)
            .order_by("source_line__movement__movement_date", "source_line_id")
        )
        self.assertEqual(len(remaining_layers), 2)
        self.assertEqual(remaining_layers[0].quantity_remaining, 2)
        self.assertEqual(remaining_layers[0].unit_cost, 10)
        self.assertEqual(remaining_layers[1].quantity_remaining, 3)
        self.assertEqual(remaining_layers[1].unit_cost, 20)

    def test_fifo_transfer_preserves_layer_costs_and_destination_issue_cost(self):
        self.product = Product.objects.create(
            company=self.company, unit=self.unit, code="FIFO-TRANSFER", name_ar="FIFO تحويل", costing_method="fifo"
        )
        source = self._warehouse("FIFO-TRANSFER-SRC")
        destination = self._warehouse("FIFO-TRANSFER-DST")

        first = self._movement("FIFO-TRANSFER-R1", "receipt", destination=source, quantity="2", cost="5")
        second = self._movement("FIFO-TRANSFER-R2", "receipt", destination=source, quantity="2", cost="9")
        for receipt in (first, second):
            self.assertEqual(receipt.status_code, 201, receipt.data)
            posted = self.client.post(f"/api/inventory/movements/{receipt.data['id']}/post/", {}, format="json")
            self.assertEqual(posted.status_code, 200, posted.data)

        transfer = self._movement("FIFO-TRANSFER-T1", "transfer", destination=destination, source=source, quantity="3")
        self.assertEqual(transfer.status_code, 201, transfer.data)
        posted_transfer = self.client.post(f"/api/inventory/movements/{transfer.data['id']}/post/", {}, format="json")
        self.assertEqual(posted_transfer.status_code, 200, posted_transfer.data)
        transfer_line = StockMovement.objects.get(pk=transfer.data["id"]).lines.get()
        self.assertEqual(transfer_line.cost_amount, 19)

        source_balance = StockBalance.objects.get(product=self.product, warehouse=source)
        destination_balance = StockBalance.objects.get(product=self.product, warehouse=destination)
        self.assertEqual(source_balance.quantity, 1)
        self.assertEqual(source_balance.inventory_value, 9)
        self.assertEqual(destination_balance.quantity, 3)
        self.assertEqual(destination_balance.inventory_value, 19)

        issue = self._movement("FIFO-TRANSFER-I1", "issue", source=destination, quantity="2")
        self.assertEqual(issue.status_code, 201, issue.data)
        posted_issue = self.client.post(f"/api/inventory/movements/{issue.data['id']}/post/", {}, format="json")
        self.assertEqual(posted_issue.status_code, 200, posted_issue.data)
        issue_line = StockMovement.objects.get(pk=issue.data["id"]).lines.get()
        self.assertEqual(issue_line.cost_amount, 10)
        destination_balance.refresh_from_db()
        self.assertEqual(destination_balance.quantity, 1)
        self.assertEqual(destination_balance.inventory_value, 9)

    def test_weighted_average_receipts_and_cross_warehouse_transfer_keep_value(self):
        self.product = Product.objects.create(
            company=self.company, unit=self.unit, code="WAVG-TRANSFER", name_ar="متوسط مرجح تحويل"
        )
        source = self._warehouse("WAVG-SRC")
        destination = self._warehouse("WAVG-DST")

        for number, cost in (("WAVG-R1", "5"), ("WAVG-R2", "9")):
            receipt = self._movement(number, "receipt", destination=source, quantity="2", cost=cost)
            self.assertEqual(receipt.status_code, 201, receipt.data)
            posted = self.client.post(f"/api/inventory/movements/{receipt.data['id']}/post/", {}, format="json")
            self.assertEqual(posted.status_code, 200, posted.data)

        source_balance = StockBalance.objects.get(product=self.product, warehouse=source)
        self.assertEqual(source_balance.quantity, 4)
        self.assertEqual(source_balance.inventory_value, 28)
        self.assertEqual(source_balance.average_cost, 7)

        transfer = self._movement("WAVG-T1", "transfer", destination=destination, source=source, quantity="2")
        self.assertEqual(transfer.status_code, 201, transfer.data)
        posted_transfer = self.client.post(f"/api/inventory/movements/{transfer.data['id']}/post/", {}, format="json")
        self.assertEqual(posted_transfer.status_code, 200, posted_transfer.data)
        transfer_line = StockMovement.objects.get(pk=transfer.data["id"]).lines.get()
        self.assertEqual(transfer_line.cost_amount, 14)

        source_balance.refresh_from_db()
        destination_balance = StockBalance.objects.get(product=self.product, warehouse=destination)
        self.assertEqual(source_balance.quantity, 2)
        self.assertEqual(source_balance.inventory_value, 14)
        self.assertEqual(source_balance.average_cost, 7)
        self.assertEqual(destination_balance.quantity, 2)
        self.assertEqual(destination_balance.inventory_value, 14)
        self.assertEqual(destination_balance.average_cost, 7)

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

    def test_inventory_gl_reconciliation_matches_posted_receipt(self):
        self.product = Product.objects.create(company=self.company, unit=self.unit, code="REC-1", name_ar="مطابقة مخزون")
        warehouse = self._warehouse("REC-WH")
        created = self._movement("REC-RCV-1", "receipt", destination=warehouse, quantity="3", cost="25")
        self.assertEqual(created.status_code, 201, created.data)
        posted = self.client.post(f"/api/inventory/movements/{created.data['id']}/post/", {}, format="json")
        self.assertEqual(posted.status_code, 200, posted.data)
        response = self.client.get(f"/api/inventory/balances/reconciliation/?company={self.company.pk}")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["stock_value"], "75.0000")
        self.assertEqual(response.data["gl_balance"], "75.0000")
        self.assertEqual(response.data["difference"], "0.0000")
        self.assertTrue(response.data["is_reconciled"])

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


    def test_closed_fiscal_year_rolls_back_stock_and_journal_posting(self):
        self.product = Product.objects.create(company=self.company, unit=self.unit, code="GL-LOCK", name_ar="منع الترحيل")
        warehouse = self._warehouse("GL-LOCK-WH")
        self.fiscal_year.status = FiscalYear.Status.CLOSED
        self.fiscal_year.save(update_fields=("status",))
        created = self._movement("GL-LOCK-R1", "receipt", destination=warehouse, quantity="2", cost="9")
        self.assertEqual(created.status_code, 201, created.data)
        response = self.client.post(f"/api/inventory/movements/{created.data['id']}/post/", {}, format="json")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(StockBalance.objects.filter(product=self.product, warehouse=warehouse).exists())
        self.assertFalse(StockMovementJournalLink.objects.filter(movement_id=created.data["id"]).exists())
        self.assertFalse(JournalEntry.objects.filter(company=self.company).exists())
        self.assertEqual(StockMovement.objects.get(pk=created.data["id"]).status, StockMovement.Status.DRAFT)
