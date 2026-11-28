from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from accounting.models import AccountRole, ChartOfAccount, JournalEntry
from companies.models import Branch, Company, CompanyMembership, Currency, FiscalPeriod, FiscalYear
from .models import InventoryCount, Product, ProductCategory, StockBalance, StockCostLayer, StockMovement, StockMovementJournalLink, UnitOfMeasure, Warehouse, WarehouseProductSetting, WarehouseStatusAudit


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

    def _movement(self, number, movement_type, destination=None, source=None, quantity="5", cost="10", source_location=None, destination_location=None, expiry_date=None, adjustment_direction="", adjustment_reason="other"):
        return self.client.post("/api/inventory/movements/", {
            "company": self.company.pk,
            "movement_number": number,
            "movement_type": movement_type,
            "adjustment_reason": adjustment_reason if movement_type == "adjustment" else "",
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

    def test_fifo_cost_aging_uses_original_receipt_date_and_remaining_layer_value(self):
        self.product = Product.objects.create(
            company=self.company, unit=self.unit, code="FIFO-AGE-1", name_ar="صنف أعمار FIFO",
            costing_method=Product.CostingMethod.FIFO,
        )
        warehouse = self._warehouse("FIFO-AGE-WH")
        created = self._movement("FIFO-AGE-RCV", "receipt", destination=warehouse, quantity="5", cost="12")
        self.assertEqual(created.status_code, 201, created.data)
        posted = self.client.post(f"/api/inventory/movements/{created.data['id']}/post/", {}, format="json")
        self.assertEqual(posted.status_code, 200, posted.data)

        report = self.client.get(
            f"/api/inventory/balances/cost-aging/?company={self.company.pk}&as_of=2026-12-31"
        )
        self.assertEqual(report.status_code, 200, report.data)
        self.assertEqual(report.data["totals"]["layers"], 1)
        self.assertEqual(report.data["totals"]["quantity"], "5.0000")
        self.assertEqual(report.data["totals"]["value"], "60.0000")
        self.assertEqual(report.data["rows"][0]["receipt_date"], "2026-11-21")
        self.assertEqual(report.data["rows"][0]["age_days"], 40)
        self.assertEqual(report.data["buckets"]["slow"]["quantity"], "5.0000")
        self.assertEqual(report.data["rows"][0]["source_movement_number"], "FIFO-AGE-RCV")

    def test_cost_aging_reconstructs_remaining_quantity_at_historical_date(self):
        self.product = Product.objects.create(
            company=self.company, unit=self.unit, code="FIFO-HIST-1", name_ar="تاريخ أعمار تاريخي",
            costing_method=Product.CostingMethod.FIFO,
        )
        warehouse = self._warehouse("FIFO-HIST-WH")
        receipt = self._movement(
            "FIFO-HIST-R", "receipt", destination=warehouse, quantity="10", cost="10",
        )
        StockMovement.objects.filter(pk=receipt.data["id"]).update(movement_date=date(2026, 1, 1))
        posted_receipt = self.client.post(
            f"/api/inventory/movements/{receipt.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(posted_receipt.status_code, 200, posted_receipt.data)

        issue_one = self._movement("FIFO-HIST-I1", "issue", source=warehouse, quantity="2")
        StockMovement.objects.filter(pk=issue_one.data["id"]).update(movement_date=date(2026, 3, 1))
        posted_issue_one = self.client.post(
            f"/api/inventory/movements/{issue_one.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(posted_issue_one.status_code, 200, posted_issue_one.data)
        issue_two = self._movement("FIFO-HIST-I2", "issue", source=warehouse, quantity="3")
        StockMovement.objects.filter(pk=issue_two.data["id"]).update(movement_date=date(2026, 4, 1))
        posted_issue_two = self.client.post(
            f"/api/inventory/movements/{issue_two.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(posted_issue_two.status_code, 200, posted_issue_two.data)

        report = self.client.get(
            f"/api/inventory/balances/cost-aging/?company={self.company.pk}&as_of=2026-02-01",
        )
        self.assertEqual(report.status_code, 200, report.data)
        self.assertEqual(report.data["totals"]["quantity"], "10.0000")
        self.assertEqual(report.data["totals"]["value"], "100.0000")
        self.assertEqual(report.data["rows"][0]["quantity_as_of"], "10.0000")

    def test_cost_aging_uses_source_receipt_for_legacy_rootless_original_layer(self):
        self.product = Product.objects.create(
            company=self.company, unit=self.unit, code="FIFO-LEGACY-1", name_ar="طبقة قديمة",
            costing_method=Product.CostingMethod.FIFO,
        )
        warehouse = self._warehouse("FIFO-LEGACY-WH")
        receipt = self._movement(
            "FIFO-LEGACY-R", "receipt", destination=warehouse, quantity="3", cost="7",
        )
        StockMovement.objects.filter(pk=receipt.data["id"]).update(movement_date=date(2026, 1, 5))
        posted = self.client.post(
            f"/api/inventory/movements/{receipt.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(posted.status_code, 200, posted.data)
        StockCostLayer.objects.filter(product=self.product, warehouse=warehouse).update(root_source_line=None)

        report = self.client.get(
            f"/api/inventory/balances/cost-aging/?company={self.company.pk}&as_of=2026-02-01",
        )
        self.assertEqual(report.status_code, 200, report.data)
        self.assertEqual(report.data["totals"]["layers"], 1)
        self.assertEqual(report.data["rows"][0]["source_movement_number"], "FIFO-LEGACY-R")
        self.assertEqual(report.data["rows"][0]["receipt_date"], "2026-01-05")

    def test_inventory_count_snapshots_balances_and_posts_loss_variance(self):
        self.product = Product.objects.create(
            company=self.company, unit=self.unit, code="COUNT-LOSS-1", name_ar="جرد عجز",
        )
        warehouse = self._warehouse("COUNT-LOSS-WH")
        receipt = self._movement("COUNT-LOSS-R", "receipt", destination=warehouse, quantity="5", cost="10")
        self.assertEqual(receipt.status_code, 201, receipt.data)
        self.assertEqual(
            self.client.post(f"/api/inventory/movements/{receipt.data['id']}/post/", {}, format="json").status_code,
            200,
        )

        started = self.client.post("/api/inventory/counts/", {
            "company": self.company.pk, "warehouse": warehouse.pk,
            "count_number": "COUNT-LOSS-001", "count_date": "2026-11-21",
        }, format="json")
        self.assertEqual(started.status_code, 201, started.data)
        self.assertEqual(len(started.data["lines"]), 1)
        line = started.data["lines"][0]
        self.assertEqual(line["system_quantity"], "5.0000")
        self.assertIsNone(line["counted_quantity"])

        updated = self.client.patch(
            f"/api/inventory/count-lines/{line['id']}/",
            {"counted_quantity": "4"},
            format="json",
        )
        self.assertEqual(updated.status_code, 200, updated.data)
        self.assertEqual(updated.data["variance_quantity"], "-1.0000")

        posted = self.client.post(
            f"/api/inventory/counts/{started.data['id']}/post-count/", {}, format="json",
        )
        self.assertEqual(posted.status_code, 200, posted.data)
        self.assertEqual(posted.data["status"], InventoryCount.Status.POSTED)
        self.assertTrue(posted.data["loss_movement_number"])
        balance = StockBalance.objects.get(product=self.product, warehouse=warehouse)
        self.assertEqual(balance.quantity, Decimal("4.0000"))
        self.assertEqual(balance.inventory_value, Decimal("40.0000"))

    def test_inventory_count_posts_positive_variance_with_explicit_cost(self):
        self.product = Product.objects.create(
            company=self.company, unit=self.unit, code="COUNT-GAIN-1", name_ar="جرد زيادة",
        )
        warehouse = self._warehouse("COUNT-GAIN-WH")
        started = self.client.post("/api/inventory/counts/", {
            "company": self.company.pk, "warehouse": warehouse.pk,
            "count_number": "COUNT-GAIN-001", "count_date": "2026-11-21",
        }, format="json")
        self.assertEqual(started.status_code, 201, started.data)
        line = self.client.post("/api/inventory/count-lines/", {
            "count": started.data["id"], "product": self.product.pk,
            "counted_quantity": "3", "adjustment_unit_cost": "2.5",
        }, format="json")
        self.assertEqual(line.status_code, 201, line.data)
        posted = self.client.post(
            f"/api/inventory/counts/{started.data['id']}/post-count/", {}, format="json",
        )
        self.assertEqual(posted.status_code, 200, posted.data)
        self.assertTrue(posted.data["gain_movement_number"])
        balance = StockBalance.objects.get(product=self.product, warehouse=warehouse)
        self.assertEqual(balance.quantity, Decimal("3.0000"))
        self.assertEqual(balance.inventory_value, Decimal("7.5000"))

    def test_manual_movement_cannot_post_while_inventory_count_is_open(self):
        self.product = Product.objects.create(
            company=self.company, unit=self.unit, code="COUNT-LOCK-1", name_ar="قفل الجرد",
        )
        warehouse = self._warehouse("COUNT-LOCK-WH")
        started = self.client.post("/api/inventory/counts/", {
            "company": self.company.pk, "warehouse": warehouse.pk,
            "count_number": "COUNT-LOCK-001", "count_date": "2026-11-21",
        }, format="json")
        self.assertEqual(started.status_code, 201, started.data)
        receipt = self._movement("COUNT-LOCK-R", "receipt", destination=warehouse, quantity="1", cost="3")
        self.assertEqual(receipt.status_code, 201, receipt.data)
        response = self.client.post(
            f"/api/inventory/movements/{receipt.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("جرد فعلي مفتوح", str(response.data))
        self.assertEqual(
            StockMovement.objects.get(pk=receipt.data["id"]).status,
            StockMovement.Status.DRAFT,
        )

    def test_inventory_count_rejects_unfinished_or_stale_snapshot(self):
        self.product = Product.objects.create(
            company=self.company, unit=self.unit, code="COUNT-STALE-1", name_ar="جرد قديم",
        )
        warehouse = self._warehouse("COUNT-STALE-WH")
        started = self.client.post("/api/inventory/counts/", {
            "company": self.company.pk, "warehouse": warehouse.pk,
            "count_number": "COUNT-STALE-001", "count_date": "2026-11-21",
        }, format="json")
        self.assertEqual(started.status_code, 201, started.data)
        line = self.client.post("/api/inventory/count-lines/", {
            "count": started.data["id"], "product": self.product.pk,
            "counted_quantity": "0",
        }, format="json")
        self.assertEqual(line.status_code, 201, line.data)
        receipt = self._movement("COUNT-STALE-R", "receipt", destination=warehouse, quantity="2", cost="4")
        self.assertEqual(receipt.status_code, 201, receipt.data)
        # A draft created after the snapshot is enough to invalidate the count;
        # it must not be posted while the count remains open.
        response = self.client.post(
            f"/api/inventory/counts/{started.data['id']}/post-count/", {}, format="json",
        )
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("حركة مخزنية بعد بدء الجرد", str(response.data))
        self.assertEqual(
            InventoryCount.objects.get(pk=started.data["id"]).status,
            InventoryCount.Status.DRAFT,
        )

    def test_only_one_open_inventory_count_is_allowed_per_warehouse(self):
        warehouse = self._warehouse("COUNT-ONE-WH")
        payload = {
            "company": self.company.pk, "warehouse": warehouse.pk,
            "count_number": "COUNT-ONE-001", "count_date": "2026-11-21",
        }
        first = self.client.post("/api/inventory/counts/", payload, format="json")
        self.assertEqual(first.status_code, 201, first.data)
        second = self.client.post("/api/inventory/counts/", {
            **payload, "count_number": "COUNT-ONE-002",
        }, format="json")
        self.assertEqual(second.status_code, 400, second.data)
        self.assertIn("جرد مفتوح", str(second.data))

    def test_inventory_count_cannot_start_while_draft_movement_exists(self):
        self.product = Product.objects.create(
            company=self.company, unit=self.unit, code="COUNT-PENDING-1", name_ar="جرد مع مسودة",
        )
        warehouse = self._warehouse("COUNT-PENDING-WH")
        draft = self._movement("COUNT-PENDING-R", "receipt", destination=warehouse)
        self.assertEqual(draft.status_code, 201, draft.data)
        response = self.client.post("/api/inventory/counts/", {
            "company": self.company.pk, "warehouse": warehouse.pk,
            "count_number": "COUNT-PENDING-001", "count_date": "2026-11-21",
        }, format="json")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("مستند حركة مسودة", str(response.data))

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
        self.assertEqual(str(StockCostLayer.objects.get(product=self.product, warehouse=warehouse, quantity_remaining__gt=0).unit_cost), "10.00000000")


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


    def test_products_support_stock_classes_and_services_have_no_stock_class(self):
        goods = self.client.post("/api/inventory/products/", {
            "company": self.company.pk, "unit": self.unit.pk,
            "code": "CLASS-RAW-1", "name_ar": "مواد خام",
            "product_type": "goods", "stock_class": "raw_material",
        }, format="json")
        self.assertEqual(goods.status_code, 201, goods.data)
        self.assertEqual(goods.data["stock_class"], "raw_material")

        service = self.client.post("/api/inventory/products/", {
            "company": self.company.pk, "unit": self.unit.pk,
            "code": "CLASS-SERVICE-1", "name_ar": "خدمة",
            "product_type": "service", "stock_class": "finished_goods",
        }, format="json")
        self.assertEqual(service.status_code, 201, service.data)
        self.assertIsNone(service.data["stock_class"])

    def test_stock_card_orders_same_day_events_by_posting_time_not_document_number(self):
        self.product = Product.objects.create(
            company=self.company, unit=self.unit, code="CARD-ORDER-1", name_ar="ترتيب بطاقة الصنف",
        )
        warehouse = self._warehouse("CARD-ORDER-WH")
        receipt = self._movement(
            "Z-RECEIPT", "receipt", destination=warehouse, quantity="5", cost="2",
        )
        self.assertEqual(receipt.status_code, 201, receipt.data)
        self.assertEqual(
            self.client.post(f"/api/inventory/movements/{receipt.data['id']}/post/", {}, format="json").status_code,
            200,
        )
        issue = self._movement("A-ISSUE", "issue", source=warehouse, quantity="2")
        self.assertEqual(issue.status_code, 201, issue.data)
        self.assertEqual(
            self.client.post(f"/api/inventory/movements/{issue.data['id']}/post/", {}, format="json").status_code,
            200,
        )
        card = self.client.get(
            f"/api/inventory/movements/stock-card/?product={self.product.pk}&warehouse={warehouse.pk}",
        )
        self.assertEqual(card.status_code, 200, card.data)
        self.assertEqual([row["movement_number"] for row in card.data["rows"]], ["Z-RECEIPT", "A-ISSUE"])
        self.assertTrue(card.data["rows"][0]["posted_at"])
        self.assertGreaterEqual(Decimal(card.data["rows"][0]["balance_quantity"]), Decimal("0"))

    def test_stock_card_paginates_without_losing_running_balance(self):
        self.product = Product.objects.create(
            company=self.company, unit=self.unit, code="CARD-PAGE-1", name_ar="ترقيم بطاقة الصنف",
        )
        warehouse = self._warehouse("CARD-PAGE-WH")
        opening = self._movement(
            "CARD-PAGE-OPEN", "receipt", destination=warehouse, quantity="4", cost="2",
        )
        self.assertEqual(opening.status_code, 201, opening.data)
        StockMovement.objects.filter(pk=opening.data["id"]).update(movement_date=date(2026, 1, 1))
        self.assertEqual(
            self.client.post(f"/api/inventory/movements/{opening.data['id']}/post/", {}, format="json").status_code,
            200,
        )
        for index in range(3):
            movement = self._movement(
                f"CARD-PAGE-{index}", "receipt", destination=warehouse, quantity="1", cost="2",
            )
            self.assertEqual(movement.status_code, 201, movement.data)
            self.assertEqual(
                self.client.post(f"/api/inventory/movements/{movement.data['id']}/post/", {}, format="json").status_code,
                200,
            )

        url = f"/api/inventory/movements/stock-card/?product={self.product.pk}&warehouse={warehouse.pk}&date_from=2026-02-01&page_size=2"
        first = self.client.get(url + "&page=1")
        second = self.client.get(url + "&page=2")
        self.assertEqual(first.status_code, 200, first.data)
        self.assertEqual(second.status_code, 200, second.data)
        self.assertEqual(len(first.data["rows"]), 2)
        self.assertEqual(len(second.data["rows"]), 1)
        self.assertEqual(first.data["pagination"]["total_rows"], 3)
        self.assertTrue(first.data["pagination"]["has_next"])
        self.assertFalse(second.data["pagination"]["has_next"])
        self.assertEqual(Decimal(first.data["opening_quantity"]), Decimal("4"))
        self.assertEqual(Decimal(first.data["rows"][0]["balance_quantity"]), Decimal("5"))
        self.assertEqual(Decimal(first.data["rows"][1]["balance_quantity"]), Decimal("6"))
        self.assertEqual(Decimal(second.data["rows"][0]["balance_quantity"]), Decimal("7"))
        self.assertEqual(Decimal(second.data["closing_quantity"]), Decimal("7"))

    def test_recent_movements_endpoint_returns_only_requested_limit(self):
        self.product = Product.objects.create(
            company=self.company, unit=self.unit, code="RECENT-API-1", name_ar="حركات حديثة",
        )
        warehouse = self._warehouse("RECENT-WH")
        for index in range(3):
            response = self._movement(
                f"RECENT-{index}", "receipt", destination=warehouse,
                quantity="1", cost="1",
            )
            self.assertEqual(response.status_code, 201, response.data)
        response = self.client.get(
            f"/api/inventory/movements/recent/?company={self.company.pk}&limit=2",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(len(response.data), 2)

    def test_stock_aging_is_calculated_server_side_from_latest_posted_movement(self):
        self.product = Product.objects.create(
            company=self.company, unit=self.unit, code="AGING-API-1", name_ar="تحليل حركة",
        )
        warehouse = self._warehouse("AGING-API-WH")
        receipt = self._movement("AGING-API-R", "receipt", destination=warehouse, quantity="5", cost="2")
        self.assertEqual(receipt.status_code, 201, receipt.data)
        StockMovement.objects.filter(pk=receipt.data["id"]).update(movement_date=date(2026, 1, 1))
        self.assertEqual(
            self.client.post(f"/api/inventory/movements/{receipt.data['id']}/post/", {}, format="json").status_code,
            200,
        )
        issue = self._movement("AGING-API-I", "issue", source=warehouse, quantity="1")
        StockMovement.objects.filter(pk=issue.data["id"]).update(movement_date=date(2026, 2, 1))
        self.assertEqual(
            self.client.post(f"/api/inventory/movements/{issue.data['id']}/post/", {}, format="json").status_code,
            200,
        )
        response = self.client.get(f"/api/inventory/balances/stock-aging/?company={self.company.pk}")
        self.assertEqual(response.status_code, 200, response.data)
        row = next(item for item in response.data["rows"] if item["product"] == self.product.pk)
        self.assertEqual(row["last_movement"], "2026-02-01")
        self.assertEqual(row["quantity"], "4.0000")

    def test_issue_rejects_expired_tracked_stock(self):
        self.product = Product.objects.create(
            company=self.company, unit=self.unit, code="EXP-ISSUE-1", name_ar="منتهي الصلاحية",
            track_expiry=True,
        )
        warehouse = self._warehouse("EXP-ISSUE-WH")
        receipt = self._movement(
            "EXP-ISSUE-R", "receipt", destination=warehouse,
            quantity="5", cost="4", expiry_date="2026-10-01",
        )
        self.assertEqual(receipt.status_code, 201, receipt.data)
        self.assertEqual(
            self.client.post(f"/api/inventory/movements/{receipt.data['id']}/post/", {}, format="json").status_code,
            200,
        )
        issue = self._movement(
            "EXP-ISSUE-I", "issue", source=warehouse,
            quantity="1", expiry_date="2026-10-01",
        )
        self.assertEqual(issue.status_code, 201, issue.data)
        response = self.client.post(
            f"/api/inventory/movements/{issue.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("منتهية الصلاحية", str(response.data))
        balance = StockBalance.objects.get(product=self.product, warehouse=warehouse)
        self.assertEqual(balance.quantity, Decimal("5.0000"))

    def test_movement_rejects_quantity_beyond_unit_precision(self):
        self.product = Product.objects.create(
            company=self.company, unit=self.unit, code="PRECISION-1", name_ar="دقة الكمية",
        )
        warehouse = self._warehouse("PRECISION-WH")
        response = self._movement(
            "PRECISION-R1", "receipt", destination=warehouse, quantity="1.234", cost="2",
        )
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("دقة وحدة القياس", str(response.data))

    def test_damage_and_writeoff_adjustments_must_reduce_stock(self):
        self.product = Product.objects.create(
            company=self.company, unit=self.unit, code="ADJ-DAMAGE-1", name_ar="تسوية تالف",
        )
        warehouse = self._warehouse("ADJ-DAMAGE-WH")
        response = self._movement(
            "ADJ-DAMAGE-1", "adjustment", destination=warehouse,
            quantity="1", cost="2", adjustment_direction="in", adjustment_reason="damage",
        )
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("تسوية عجز", str(response.data))

    def test_adjustment_requires_a_documented_reason(self):
        self.product = Product.objects.create(
            company=self.company, unit=self.unit, code="ADJ-REASON-1", name_ar="سبب التسوية",
        )
        warehouse = self._warehouse("ADJ-REASON-WH")
        response = self.client.post("/api/inventory/movements/", {
            "company": self.company.pk, "movement_number": "ADJ-REASON-1",
            "movement_type": "adjustment", "movement_date": "2026-11-21",
            "destination_warehouse": warehouse.pk,
            "lines": [{
                "product": self.product.pk, "quantity": "1", "unit_cost": "2",
                "adjustment_direction": "in",
            }],
        }, format="json")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("سبب التسوية", str(response.data))

    def test_adjustment_direction_must_match_the_selected_warehouse(self):
        self.product = Product.objects.create(
            company=self.company, unit=self.unit, code="ADJ-DIR-1", name_ar="اتجاه التسوية",
        )
        warehouse = self._warehouse("ADJ-DIR-WH")
        response = self._movement(
            "ADJ-DIR-1", "adjustment", destination=warehouse,
            quantity="1", cost="2", adjustment_direction="out",
        )
        self.assertEqual(response.status_code, 400, response.data)

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


    def test_inactive_warehouse_keeps_historical_stock_card_but_rejects_new_movements(self):
        self.product = Product.objects.create(
            company=self.company, unit=self.unit, code="INACTIVE-WH-HISTORY",
            name_ar="تاريخ مخزن موقوف",
        )
        warehouse = self._warehouse("INACTIVE-HISTORY-WH")
        receipt = self._movement(
            "INACTIVE-HISTORY-RCV", "receipt", destination=warehouse,
            quantity="5", cost="10",
        )
        self.assertEqual(receipt.status_code, 201, receipt.data)
        posted = self.client.post(
            f"/api/inventory/movements/{receipt.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(posted.status_code, 200, posted.data)

        disabled = self.client.delete(f"/api/inventory/warehouses/{warehouse.pk}/")
        self.assertEqual(disabled.status_code, 204)
        warehouse.refresh_from_db()
        self.assertFalse(warehouse.is_active)

        historical_card = self.client.get(
            f"/api/inventory/movements/stock-card/?product={self.product.pk}"
            f"&warehouse={warehouse.pk}&date_from=2026-11-01&date_to=2026-11-30",
        )
        self.assertEqual(historical_card.status_code, 200, historical_card.data)
        self.assertEqual(historical_card.data["closing_quantity"], "5.0000")
        self.assertEqual(historical_card.data["closing_value"], "50.0000")
        self.assertEqual(
            [row["movement_number"] for row in historical_card.data["rows"]],
            ["INACTIVE-HISTORY-RCV"],
        )

        blocked = self._movement(
            "INACTIVE-HISTORY-NEW", "receipt", destination=warehouse,
            quantity="1", cost="10",
        )
        self.assertEqual(blocked.status_code, 400, blocked.data)
        self.assertIn("مخزن موقوف", str(blocked.data))

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

    def test_stock_card_orders_backdated_postings_by_movement_date(self):
        self.product = Product.objects.create(
            company=self.company, unit=self.unit, code="CARD-DATE-1", name_ar="ترتيب بطاقة الصنف"
        )
        warehouse = self._warehouse("CARD-DATE-WH")

        first = self._movement("CARD-DATE-R1", "receipt", destination=warehouse, quantity="4", cost="10")
        self.assertEqual(first.status_code, 201, first.data)
        self.assertEqual(
            self.client.post(f"/api/inventory/movements/{first.data['id']}/post/", {}, format="json").status_code,
            200,
        )

        issue = self._movement("CARD-DATE-I1", "issue", source=warehouse, quantity="1")
        self.assertEqual(issue.status_code, 201, issue.data)
        issue_record = StockMovement.objects.get(pk=issue.data["id"])
        issue_record.movement_date = date(2026, 11, 23)
        issue_record.save(update_fields=("movement_date",))
        self.assertEqual(
            self.client.post(f"/api/inventory/movements/{issue.data['id']}/post/", {}, format="json").status_code,
            200,
        )

        backdated = self._movement("CARD-DATE-R2", "receipt", destination=warehouse, quantity="2", cost="12")
        self.assertEqual(backdated.status_code, 201, backdated.data)
        backdated_record = StockMovement.objects.get(pk=backdated.data["id"])
        backdated_record.movement_date = date(2026, 11, 22)
        backdated_record.save(update_fields=("movement_date",))
        self.assertEqual(
            self.client.post(f"/api/inventory/movements/{backdated.data['id']}/post/", {}, format="json").status_code,
            200,
        )

        response = self.client.get(
            f"/api/inventory/movements/stock-card/?product={self.product.pk}&warehouse={warehouse.pk}&date_from=2026-11-20&date_to=2026-11-30"
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            [row["movement_number"] for row in response.data["rows"]],
            ["CARD-DATE-R1", "CARD-DATE-R2", "CARD-DATE-I1"],
        )

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

    def test_fifo_valuation_integrity_detects_balance_layer_value_mismatch(self):
        self.product = Product.objects.create(
            company=self.company, unit=self.unit, code="FIFO-INTEGRITY-1",
            name_ar="فحص تقييم FIFO", costing_method=Product.CostingMethod.FIFO,
        )
        warehouse = self._warehouse("FIFO-INTEGRITY-WH")
        receipt = self._movement(
            "FIFO-INTEGRITY-R", "receipt", destination=warehouse, quantity="4", cost="10",
        )
        self.assertEqual(receipt.status_code, 201, receipt.data)
        posted = self.client.post(
            f"/api/inventory/movements/{receipt.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(posted.status_code, 200, posted.data)

        url = f"/api/inventory/balances/valuation-integrity/?company={self.company.pk}"
        matched = self.client.get(url)
        self.assertEqual(matched.status_code, 200, matched.data)
        self.assertEqual(matched.data["fifo_mismatch_count"], 0)

        balance = StockBalance.objects.get(product=self.product, warehouse=warehouse)
        balance.inventory_value += Decimal("1.0000")
        balance.save(update_fields=("inventory_value",))
        mismatch = self.client.get(url)
        self.assertEqual(mismatch.status_code, 200, mismatch.data)
        self.assertEqual(mismatch.data["fifo_mismatch_count"], 1)
        self.assertEqual(len(mismatch.data["fifo_mismatches"]), 1)
        self.assertEqual(mismatch.data["fifo_mismatches"][0]["value_difference"], "1.0000")

    def test_inventory_reports_and_stock_card_reject_unassigned_branch_scope(self):
        restricted_branch = Branch.objects.create(
            company=self.company, code="B3", name_ar="فرع غير مصرح به",
        )
        product = Product.objects.create(
            company=self.company, unit=self.unit, code="REPORT-SCOPE-PROD", name_ar="صنف اختبار الصلاحيات",
        )
        restricted_warehouse = Warehouse.objects.create(
            company=self.company, branch=restricted_branch,
            code="REPORT-HIDDEN-WH", name_ar="مخزن فرع غير مصرح به",
        )
        limited_user = get_user_model().objects.create_user(
            username="inventory-report-branch-limited", password="test-password",
        )
        membership = CompanyMembership.objects.create(
            user=limited_user, company=self.company, role="accountant", all_branches=False,
        )
        membership.branches.add(self.branch)
        self.client.force_authenticate(limited_user)

        for report in ("stock-aging", "cost-aging", "reconciliation"):
            response = self.client.get(
                f"/api/inventory/balances/{report}/?company={self.company.pk}&branch={restricted_branch.pk}",
            )
            self.assertEqual(response.status_code, 403, (report, response.data))

        card = self.client.get(
            f"/api/inventory/movements/stock-card/?product={product.pk}&warehouse={restricted_warehouse.pk}",
        )
        self.assertEqual(card.status_code, 404, card.data)

    def test_inventory_integrity_report_rejects_an_unassigned_branch_scope(self):
        restricted_branch = Branch.objects.create(
            company=self.company, code="B2-INTEGRITY", name_ar="فرع غير مسموح",
        )
        limited_user = get_user_model().objects.create_user(
            username="inventory-integrity-limited", password="test-password",
        )
        membership = CompanyMembership.objects.create(
            user=limited_user, company=self.company, role="accountant", all_branches=False,
        )
        membership.branches.add(self.branch)
        self.client.force_authenticate(limited_user)

        response = self.client.get(
            f"/api/inventory/balances/valuation-integrity/?company={self.company.pk}&branch={restricted_branch.pk}",
        )
        self.assertEqual(response.status_code, 403, response.data)

    def test_reorder_suggestions_calculate_gap_from_available_stock(self):
        self.product = Product.objects.create(
            company=self.company, unit=self.unit, code="REORDER-001", name_ar="صنف إعادة طلب",
        )
        warehouse = self._warehouse("REORDER-WH")
        WarehouseProductSetting.objects.create(
            product=self.product, warehouse=warehouse, min_stock=Decimal("10"),
        )
        receipt = self._movement(
            "REORDER-RCV", "receipt", destination=warehouse, quantity="3", cost="5",
        )
        self.assertEqual(receipt.status_code, 201, receipt.data)
        posted = self.client.post(
            f"/api/inventory/movements/{receipt.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(posted.status_code, 200, posted.data)

        report = self.client.get(
            f"/api/inventory/balances/reorder-suggestions/?company={self.company.pk}",
        )
        self.assertEqual(report.status_code, 200, report.data)
        row = next(item for item in report.data["rows"] if item["product"] == self.product.pk)
        self.assertEqual(row["on_hand"], "3.0000")
        self.assertEqual(row["reorder_point"], "10.0000")
        self.assertEqual(row["suggested_purchase_quantity"], "7.0000")
        self.assertEqual(row["stock_status"], "reorder")

    def test_pending_movements_report_lists_draft_stock_documents(self):
        self.product = Product.objects.create(
            company=self.company, unit=self.unit, code="PENDING-001", name_ar="صنف حركة معلقة",
        )
        warehouse = self._warehouse("PENDING-WH")
        draft = self._movement(
            "PENDING-MOVE-001", "receipt", destination=warehouse, quantity="2", cost="4",
        )
        self.assertEqual(draft.status_code, 201, draft.data)
        report = self.client.get(
            f"/api/inventory/movements/pending/?company={self.company.pk}",
        )
        self.assertEqual(report.status_code, 200, report.data)
        self.assertEqual(report.data["count"], 1)
        self.assertEqual(report.data["results"][0]["movement_number"], "PENDING-MOVE-001")


    def test_warehouse_reopening_is_admin_only_and_audited(self):
        warehouse = Warehouse.objects.create(
            company=self.company, branch=self.branch, code="WH-REOPEN",
            name_ar="مخزن متوقف", is_active=False,
        )
        accountant = get_user_model().objects.create_user(
            username="warehouse_reopen_accountant", password="test-password",
        )
        CompanyMembership.objects.create(
            user=accountant, company=self.company, role="accountant", all_branches=True,
        )
        self.client.force_authenticate(accountant)
        denied = self.client.patch(
            f"/api/inventory/warehouses/{warehouse.pk}/",
            {"is_active": True}, format="json",
        )
        self.assertEqual(denied.status_code, 403, denied.data)
        self.assertFalse(Warehouse.objects.get(pk=warehouse.pk).is_active)

        self.client.force_authenticate(self.user)
        reopened = self.client.patch(
            f"/api/inventory/warehouses/{warehouse.pk}/",
            {"is_active": True}, format="json",
        )
        self.assertEqual(reopened.status_code, 200, reopened.data)
        self.assertTrue(Warehouse.objects.get(pk=warehouse.pk).is_active)
        audit = WarehouseStatusAudit.objects.get(warehouse=warehouse)
        self.assertFalse(audit.previous_is_active)
        self.assertTrue(audit.new_is_active)
        self.assertEqual(audit.changed_by, self.user)


    def test_stock_movement_posting_is_rejected_in_a_closed_fiscal_period(self):
        self.product = Product.objects.create(
            company=self.company, unit=self.unit, code="CLOSED-PERIOD-INV",
            name_ar="مخزون فترة مغلقة",
        )
        warehouse = self._warehouse("CLOSED-PERIOD-WH")
        FiscalPeriod.objects.create(
            fiscal_year=self.fiscal_year, name="November",
            start_date=date(2026, 11, 1), end_date=date(2026, 11, 30),
            status=FiscalPeriod.Status.CLOSED,
        )
        movement = self._movement(
            "CLOSED-PERIOD-MOVE", "receipt", destination=warehouse, quantity="2", cost="5",
        )
        self.assertEqual(movement.status_code, 201, movement.data)
        posted = self.client.post(
            f"/api/inventory/movements/{movement.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(posted.status_code, 400, posted.data)
        self.assertEqual(
            StockMovement.objects.get(pk=movement.data["id"]).status,
            StockMovement.Status.DRAFT,
        )
        self.assertFalse(StockBalance.objects.filter(product=self.product, warehouse=warehouse).exists())


    def test_branch_scoped_transfer_allows_sender_dispatch_and_receiver_partial_receipt(self):
        self.product = Product.objects.create(
            company=self.company, unit=self.unit, code="BRANCH-TRF-API",
            name_ar="تحويل بين فرعين",
        )
        source = self._warehouse("BRANCH-TRF-SOURCE")
        destination_branch = Branch.objects.create(
            company=self.company, code="B-DEST", name_ar="فرع الاستلام",
        )
        destination = Warehouse.objects.create(
            company=self.company, branch=destination_branch,
            code="BRANCH-TRF-DEST", name_ar="مخزن الاستلام",
        )

        stock = self._movement(
            "BRANCH-TRF-OPENING", "receipt", destination=source,
            quantity="5", cost="8",
        )
        self.assertEqual(stock.status_code, 201, stock.data)
        posted_stock = self.client.post(
            f"/api/inventory/movements/{stock.data['id']}/post/", {}, format="json",
        )
        self.assertEqual(posted_stock.status_code, 200, posted_stock.data)

        sender = get_user_model().objects.create_user(
            username="branch_transfer_sender", password="test-password",
        )
        sender_membership = CompanyMembership.objects.create(
            user=sender, company=self.company, role="accountant", all_branches=False,
        )
        sender_membership.branches.add(self.branch)

        receiver = get_user_model().objects.create_user(
            username="branch_transfer_receiver", password="test-password",
        )
        receiver_membership = CompanyMembership.objects.create(
            user=receiver, company=self.company, role="accountant", all_branches=False,
        )
        receiver_membership.branches.add(destination_branch)

        self.client.force_authenticate(sender)
        created = self.client.post("/api/inventory/transfers/", {
            "company": self.company.pk,
            "transfer_number": "BRANCH-TRF-001",
            "transfer_date": "2026-11-21",
            "source_warehouse": source.pk,
            "destination_warehouse": destination.pk,
            "lines": [{"product": self.product.pk, "quantity": "4"}],
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        transfer_id = created.data["id"]
        transfer_line_id = created.data["lines"][0]["id"]

        sender_can_read = self.client.get(f"/api/inventory/transfers/{transfer_id}/")
        self.assertEqual(sender_can_read.status_code, 200, sender_can_read.data)
        dispatched = self.client.post(
            f"/api/inventory/transfers/{transfer_id}/dispatch/", {}, format="json",
        )
        self.assertEqual(dispatched.status_code, 200, dispatched.data)

        self.client.force_authenticate(receiver)
        receiver_can_read = self.client.get(f"/api/inventory/transfers/{transfer_id}/")
        self.assertEqual(receiver_can_read.status_code, 200, receiver_can_read.data)
        receiver_cannot_dispatch = self.client.post(
            f"/api/inventory/transfers/{transfer_id}/dispatch/", {}, format="json",
        )
        self.assertEqual(receiver_cannot_dispatch.status_code, 403, receiver_cannot_dispatch.data)

        partial_receipt = self.client.post(
            f"/api/inventory/transfers/{transfer_id}/receive/",
            {
                "receipt_date": "2026-11-22",
                "lines": [{"transfer_line": transfer_line_id, "quantity": "2"}],
            },
            format="json",
        )
        self.assertEqual(partial_receipt.status_code, 201, partial_receipt.data)
        self.assertEqual(partial_receipt.data["lines"][0]["quantity"], "2.0000")

        self.client.force_authenticate(sender)
        sender_cannot_receive = self.client.post(
            f"/api/inventory/transfers/{transfer_id}/receive/",
            {
                "receipt_date": "2026-11-23",
                "lines": [{"transfer_line": transfer_line_id, "quantity": "1"}],
            },
            format="json",
        )
        self.assertEqual(sender_cannot_receive.status_code, 403, sender_cannot_receive.data)

        sender_receipt_history = self.client.get(
            f"/api/inventory/transfer-receipts/?transfer={transfer_id}",
        )
        self.assertEqual(sender_receipt_history.status_code, 200, sender_receipt_history.data)
        receipt_rows = (
            sender_receipt_history.data["results"]
            if isinstance(sender_receipt_history.data, dict)
            else sender_receipt_history.data
        )
        self.assertEqual(len(receipt_rows), 1)
        self.assertEqual(receipt_rows[0]["receipt_number"], f"TR-{transfer_id}-R1")


class InventoryValuationNRVTests(APITestCase):
    def setUp(self):
        currency = Currency.objects.create(code="EGP", name_ar="جنيه مصري")
        self.company = Company.objects.create(code="NRV", name_ar="شركة تقييم المخزون", country="EG", activity="commercial", base_currency=currency)
        self.branch = Branch.objects.create(company=self.company, code="MAIN", name_ar="الرئيسي", is_head_office=True)
        self.year = FiscalYear.objects.create(company=self.company, name="2026", start_date=date(2026, 1, 1), end_date=date(2026, 12, 31))
        self.user = get_user_model().objects.create_user(username="nrv_user", password="test-password")
        CompanyMembership.objects.create(user=self.user, company=self.company, role="admin", all_branches=True)
        self.client.force_authenticate(self.user)
        self.unit = UnitOfMeasure.objects.create(company=self.company, code="PCS", name_ar="قطعة")
        self.product = Product.objects.create(company=self.company, unit=self.unit, code="NRV-001", name_ar="صنف تقييم", costing_method="fifo")
        self.warehouse = Warehouse.objects.create(company=self.company, branch=self.branch, code="NRV-WH", name_ar="مخزن تقييم")
        inventory = ChartOfAccount.objects.create(company=self.company, code="1000", name_ar="المخزون", nature=ChartOfAccount.Nature.ASSET)
        loss = ChartOfAccount.objects.create(company=self.company, code="5900", name_ar="خسائر تخفيض المخزون", nature=ChartOfAccount.Nature.EXPENSE)
        AccountRole.objects.create(company=self.company, role=AccountRole.Role.INVENTORY, account=inventory)
        AccountRole.objects.create(company=self.company, role=AccountRole.Role.INVENTORY_ADJUSTMENT_LOSS, account=loss)

    def _receive_stock(self):
        created = self.client.post("/api/inventory/movements/", {
            "company": self.company.pk, "movement_number": "NRV-RCV-001", "movement_type": "receipt",
            "movement_date": "2026-03-10", "destination_warehouse": self.warehouse.pk,
            "lines": [{"product": self.product.pk, "quantity": "5", "unit_cost": "10"}],
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        posted = self.client.post(f"/api/inventory/movements/{created.data['id']}/post/", {}, format="json")
        self.assertEqual(posted.status_code, 200, posted.data)
        return StockBalance.objects.get(product=self.product, warehouse=self.warehouse)

    def _assessment(self, number, nrv, day):
        created = self.client.post("/api/inventory/valuation-assessments/", {
            "company": self.company.pk, "product": self.product.pk, "warehouse": self.warehouse.pk,
            "assessment_number": number, "assessment_date": day,
            "net_realizable_value_per_unit": nrv,
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        posted = self.client.post(f"/api/inventory/valuation-assessments/{created.data['id']}/post/", {}, format="json")
        self.assertEqual(posted.status_code, 200, posted.data)
        return posted.data

    def test_nrv_write_down_partial_reversal_and_full_document_reversal_reconcile_to_gl(self):
        balance = self._receive_stock()
        first = self._assessment("NRV-001", "8", "2026-03-11")
        balance.refresh_from_db()
        self.assertEqual(balance.inventory_value, Decimal("40.00"))
        self.assertEqual(Decimal(first["adjustment_amount"]), Decimal("10.00"))
        first_entry = JournalEntry.objects.get(pk=first["journal_entry"])
        self.assertEqual(first_entry.status, JournalEntry.Status.POSTED)
        self.assertEqual(first_entry.lines.aggregate(debit=Sum("debit"))["debit"], Decimal("10.00"))

        second = self._assessment("NRV-002", "9", "2026-03-12")
        balance.refresh_from_db()
        self.assertEqual(balance.inventory_value, Decimal("45.00"))
        self.assertEqual(Decimal(second["adjustment_amount"]), Decimal("-5.00"))

        reversed_second = self.client.post(
            f"/api/inventory/valuation-assessments/{second['id']}/reverse/",
            {"entry_date": "2026-03-13"}, format="json",
        )
        self.assertEqual(reversed_second.status_code, 200, reversed_second.data)
        balance.refresh_from_db()
        self.assertEqual(balance.inventory_value, Decimal("40.00"))

        reversed_first = self.client.post(
            f"/api/inventory/valuation-assessments/{first['id']}/reverse/",
            {"entry_date": "2026-03-14"}, format="json",
        )
        self.assertEqual(reversed_first.status_code, 200, reversed_first.data)
        balance.refresh_from_db()
        self.assertEqual(balance.inventory_value, Decimal("50.00"))
        self.assertEqual(balance.average_cost, Decimal("10.000000"))

    def test_nrv_reversal_is_blocked_after_later_stock_movement(self):
        self._receive_stock()
        assessment = self._assessment("NRV-LATER-MOVE", "8", "2026-03-11")
        issue = self.client.post("/api/inventory/movements/", {
            "company": self.company.pk, "movement_number": "NRV-ISSUE-001", "movement_type": "issue",
            "movement_date": "2026-03-12", "source_warehouse": self.warehouse.pk,
            "lines": [{"product": self.product.pk, "quantity": "1"}],
        }, format="json")
        self.assertEqual(issue.status_code, 201, issue.data)
        posted_issue = self.client.post(f"/api/inventory/movements/{issue.data['id']}/post/", {}, format="json")
        self.assertEqual(posted_issue.status_code, 200, posted_issue.data)
        rejected = self.client.post(
            f"/api/inventory/valuation-assessments/{assessment['id']}/reverse/",
            {"entry_date": "2026-03-13"}, format="json",
        )
        self.assertEqual(rejected.status_code, 400, rejected.data)
