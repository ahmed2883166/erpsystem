from datetime import date
from decimal import Decimal

from django.db.models import Sum

from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate
from rest_framework.exceptions import ValidationError

from accounts.models import User
from accounting.models import AccountRole, ChartOfAccount, JournalEntry, JournalLine
from companies.models import Branch, Company, CompanyMembership, Currency, FiscalYear

from .models import (
    Product, StockBalance, StockCostLayer, StockMovement, StockMovementLine, StorageLocation, UnitOfMeasure, Warehouse,
)
from .api import StockBalanceViewSet
from .serializers import ProductSerializer
from .services import post_stock_movement


class StockPostingServiceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        currency = Currency.objects.create(code="EGP", name_ar="جنيه مصري")
        cls.company = Company.objects.create(
            code="INV-TST", name_ar="شركة اختبار المخزون", country="EG",
            activity="commercial", base_currency=currency,
        )
        cls.branch = Branch.objects.create(
            company=cls.company, code="HQ", name_ar="الرئيسي", is_head_office=True,
        )
        cls.year = FiscalYear.objects.create(
            company=cls.company, name="2026",
            start_date=date(2026, 1, 1), end_date=date(2026, 12, 31),
        )
        cls.user = User.objects.create_user(username="inventory-tester", password="test-password")
        cls.inventory_account = cls._account("1200", "المخزون", ChartOfAccount.Nature.ASSET)
        cls.grni_account = cls._account("2100", "بضاعة مستلمة غير مفوترة", ChartOfAccount.Nature.LIABILITY)
        cls.cogs_account = cls._account("5100", "تكلفة المبيعات", ChartOfAccount.Nature.EXPENSE)
        cls.gain_account = cls._account("4900", "أرباح تسوية المخزون", ChartOfAccount.Nature.REVENUE)
        cls.loss_account = cls._account("5900", "خسائر تسوية المخزون", ChartOfAccount.Nature.EXPENSE)
        for role, account in (
            (AccountRole.Role.INVENTORY, cls.inventory_account),
            (AccountRole.Role.GOODS_RECEIVED_NOT_INVOICED, cls.grni_account),
            (AccountRole.Role.COGS, cls.cogs_account),
            (AccountRole.Role.INVENTORY_ADJUSTMENT_GAIN, cls.gain_account),
            (AccountRole.Role.INVENTORY_ADJUSTMENT_LOSS, cls.loss_account),
        ):
            AccountRole.objects.create(company=cls.company, role=role, account=account)
        cls.unit = UnitOfMeasure.objects.create(
            company=cls.company, code="EA", name_ar="قطعة", decimal_places=2,
        )
        cls.product = Product.objects.create(
            company=cls.company, unit=cls.unit, code="ITEM-01", name_ar="صنف اختبار",
            costing_method=Product.CostingMethod.WEIGHTED_AVERAGE,
        )
        cls.warehouse = Warehouse.objects.create(
            company=cls.company, branch=cls.branch, code="WH-01", name_ar="المخزن الرئيسي",
        )

    @classmethod
    def _account(cls, code, name, nature):
        return ChartOfAccount.objects.create(
            company=cls.company, code=code, name_ar=name, nature=nature,
        )

    def _movement(
        self, number, kind, *, source=None, destination=None, quantity="1", unit_cost="0",
        direction="", source_location=None, destination_location=None, expiry_date=None,
    ):
        movement = StockMovement.objects.create(
            company=self.company, movement_number=number, movement_type=kind,
            movement_date=date(2026, 2, 1), source_warehouse=source,
            destination_warehouse=destination, created_by=self.user,
        )
        StockMovementLine.objects.create(
            movement=movement, product=self.product, quantity=Decimal(quantity),
            unit_cost=Decimal(unit_cost), adjustment_direction=direction,
            source_location=source_location, destination_location=destination_location,
            expiry_date=expiry_date,
        )
        return movement

    def test_receipt_and_issue_update_balance_and_post_journals(self):
        receipt = self._movement(
            "RCV-001", StockMovement.MovementType.RECEIPT,
            destination=self.warehouse, quantity="10", unit_cost="5",
        )
        post_stock_movement(receipt.pk, self.user)

        issue = self._movement(
            "ISS-001", StockMovement.MovementType.ISSUE,
            source=self.warehouse, quantity="3",
        )
        post_stock_movement(issue.pk, self.user)

        balance = StockBalance.objects.get(product=self.product, warehouse=self.warehouse)
        self.assertEqual(balance.quantity, Decimal("7"))
        self.assertEqual(balance.inventory_value, Decimal("35.0000"))
        self.assertEqual(receipt.status, StockMovement.Status.POSTED)
        self.assertEqual(issue.status, StockMovement.Status.POSTED)
        self.assertEqual(receipt.journal_links.count(), 1)
        self.assertEqual(issue.journal_links.count(), 1)
        self.assertTrue(all(
            link.journal_entry.status == JournalEntry.Status.POSTED
            for movement in (receipt, issue)
            for link in movement.journal_links.select_related("journal_entry")
        ))

    def test_issue_cannot_create_negative_stock_and_rolls_back_balance(self):
        issue = self._movement(
            "ISS-EMPTY", StockMovement.MovementType.ISSUE,
            source=self.warehouse, quantity="1",
        )

        with self.assertRaises(ValidationError):
            post_stock_movement(issue.pk, self.user)

        issue.refresh_from_db()
        self.assertEqual(issue.status, StockMovement.Status.DRAFT)
        self.assertFalse(StockBalance.objects.filter(
            product=self.product, warehouse=self.warehouse,
        ).exists())
        self.assertFalse(issue.journal_links.exists())

    def test_inventory_gain_adjustment_posts_to_gain_account(self):
        adjustment = self._movement(
            "ADJ-GAIN", StockMovement.MovementType.ADJUSTMENT,
            destination=self.warehouse, quantity="2", unit_cost="4",
            direction=StockMovementLine.AdjustmentDirection.IN,
        )

        post_stock_movement(adjustment.pk, self.user)

        balance = StockBalance.objects.get(product=self.product, warehouse=self.warehouse)
        self.assertEqual(balance.quantity, Decimal("2"))
        self.assertEqual(balance.inventory_value, Decimal("8.0000"))
        link = adjustment.journal_links.select_related("journal_entry").get()
        self.assertEqual(link.journal_entry.status, JournalEntry.Status.POSTED)
        lines = list(link.journal_entry.lines.order_by("id"))
        self.assertEqual(lines[0].account_id, self.inventory_account.pk)
        self.assertEqual(lines[1].account_id, self.gain_account.pk)


    def test_product_costing_method_and_type_cannot_change_after_posted_history_when_empty(self):
        receipt = self._movement(
            "METHOD-HISTORY-R", StockMovement.MovementType.RECEIPT,
            destination=self.warehouse, quantity="2", unit_cost="4",
        )
        post_stock_movement(receipt.pk, self.user)
        issue = self._movement(
            "METHOD-HISTORY-I", StockMovement.MovementType.ISSUE,
            source=self.warehouse, quantity="2",
        )
        post_stock_movement(issue.pk, self.user)

        balance = StockBalance.objects.get(product=self.product, warehouse=self.warehouse)
        self.assertEqual(balance.quantity, Decimal("0.0000"))
        self.assertTrue(
            self.product.stock_movement_lines.filter(
                movement__status=StockMovement.Status.POSTED,
            ).exists()
        )

        costing_serializer = ProductSerializer(
            self.product, data={"costing_method": Product.CostingMethod.FIFO}, partial=True,
        )
        self.assertFalse(costing_serializer.is_valid())
        self.assertIn("حركات مخزنية مرحلة", str(costing_serializer.errors))

        service_serializer = ProductSerializer(
            self.product, data={"product_type": Product.ProductType.SERVICE}, partial=True,
        )
        self.assertFalse(service_serializer.is_valid())
        self.assertIn("حركات مخزنية مرحلة", str(service_serializer.errors))

    def test_fifo_issue_consumes_oldest_cost_layers_first(self):
        self.product.costing_method = Product.CostingMethod.FIFO
        self.product.save(update_fields=("costing_method",))

        first_receipt = self._movement(
            "FIFO-R1", StockMovement.MovementType.RECEIPT,
            destination=self.warehouse, quantity="5", unit_cost="20",
        )
        second_receipt = self._movement(
            "FIFO-R2", StockMovement.MovementType.RECEIPT,
            destination=self.warehouse, quantity="5", unit_cost="10",
        )
        post_stock_movement(first_receipt.pk, self.user)
        post_stock_movement(second_receipt.pk, self.user)

        issue = self._movement(
            "FIFO-I1", StockMovement.MovementType.ISSUE,
            source=self.warehouse, quantity="6",
        )
        post_stock_movement(issue.pk, self.user)

        issue_line = issue.lines.get()
        balance = StockBalance.objects.get(product=self.product, warehouse=self.warehouse)
        layers = list(
            self.product.cost_layers.filter(warehouse=self.warehouse)
            .order_by("unit_cost").values_list("quantity_remaining", "unit_cost")
        )
        self.assertEqual(issue_line.cost_amount, Decimal("110.0000"))
        self.assertEqual(balance.quantity, Decimal("4"))
        self.assertEqual(balance.inventory_value, Decimal("40.0000"))
        self.assertEqual(layers, [(Decimal("4.0000"), Decimal("10.0000")),
                                  (Decimal("0.0000"), Decimal("20.0000"))])

    def test_cross_branch_transfer_preserves_fifo_cost_and_posts_balanced_interbranch_entries(self):
        self.product.costing_method = Product.CostingMethod.FIFO
        self.product.save(update_fields=("costing_method",))
        destination_branch = Branch.objects.create(
            company=self.company, code="BR-02", name_ar="الفرع الثاني",
        )
        destination_warehouse = Warehouse.objects.create(
            company=self.company, branch=destination_branch,
            code="WH-02", name_ar="مخزن الفرع الثاني",
        )
        clearing = self._account(
            "2190", "تسوية بين الفروع", ChartOfAccount.Nature.LIABILITY,
        )
        AccountRole.objects.create(
            company=self.company, role=AccountRole.Role.INTER_BRANCH_CLEARING,
            account=clearing,
        )
        receipt = self._movement(
            "TR-R1", StockMovement.MovementType.RECEIPT,
            destination=self.warehouse, quantity="8", unit_cost="12",
        )
        post_stock_movement(receipt.pk, self.user)

        transfer = self._movement(
            "TR-X1", StockMovement.MovementType.TRANSFER,
            source=self.warehouse, destination=destination_warehouse, quantity="5",
        )
        post_stock_movement(transfer.pk, self.user)

        source_balance = StockBalance.objects.get(product=self.product, warehouse=self.warehouse)
        destination_balance = StockBalance.objects.get(product=self.product, warehouse=destination_warehouse)
        self.assertEqual(source_balance.quantity, Decimal("3"))
        self.assertEqual(destination_balance.quantity, Decimal("5"))
        self.assertEqual(source_balance.inventory_value, Decimal("36.0000"))
        self.assertEqual(destination_balance.inventory_value, Decimal("60.0000"))
        self.assertEqual(transfer.lines.get().cost_amount, Decimal("60.0000"))

        linked_entries = list(
            transfer.journal_links.select_related("journal_entry__branch").order_by("journal_entry__entry_number")
        )
        self.assertEqual(len(linked_entries), 2)
        self.assertEqual(
            {link.journal_entry.branch_id for link in linked_entries},
            {self.branch.pk, destination_branch.pk},
        )
        self.assertTrue(all(link.journal_entry.status == JournalEntry.Status.POSTED for link in linked_entries))
        clearing_totals = []
        for link in linked_entries:
            totals = link.journal_entry.lines.filter(account=clearing).aggregate(
                debit=Sum("debit"),
                credit=Sum("credit"),
            )
            clearing_totals.append(Decimal(totals["debit"] or 0) - Decimal(totals["credit"] or 0))
        self.assertEqual(sum(clearing_totals), Decimal("0.0000"))

        # Verify that the stock snapshot and posted inventory account agree
        # independently for both branches and in the consolidated company view.
        self.user.is_superuser = True
        self.user.save(update_fields=("is_superuser",))
        factory = APIRequestFactory()

        def reconcile(branch_id=None):
            query = f"?company={self.company.pk}"
            if branch_id is not None:
                query += f"&branch={branch_id}"
            request = factory.get(f"/api/inventory/balances/reconciliation/{query}")
            force_authenticate(request, user=self.user)
            return StockBalanceViewSet.as_view({"get": "reconciliation"})(request)

        source_report = reconcile(self.branch.pk)
        destination_report = reconcile(destination_branch.pk)
        company_report = reconcile()
        for report, expected in (
            (source_report, Decimal("36.0000")),
            (destination_report, Decimal("60.0000")),
            (company_report, Decimal("96.0000")),
        ):
            self.assertEqual(report.status_code, 200)
            self.assertEqual(Decimal(report.data["stock_value"]), expected)
            self.assertEqual(Decimal(report.data["gl_balance"]), expected)
            self.assertEqual(Decimal(report.data["difference"]), Decimal("0.0000"))
            self.assertTrue(report.data["is_reconciled"])


    def test_fifo_transfer_preserves_location_expiry_and_cost_layers_across_branches(self):
        self.product.costing_method = Product.CostingMethod.FIFO
        self.product.track_expiry = True
        self.product.save(update_fields=("costing_method", "track_expiry"))

        destination_branch = Branch.objects.create(
            company=self.company, code="BR-LOT", name_ar="فرع التحويل",
        )
        destination_warehouse = Warehouse.objects.create(
            company=self.company, branch=destination_branch,
            code="WH-LOT", name_ar="مخزن التحويل",
        )
        source_location_a = StorageLocation.objects.create(
            warehouse=self.warehouse, code="SRC-A", name_ar="موقع مصدر أ",
        )
        source_location_b = StorageLocation.objects.create(
            warehouse=self.warehouse, code="SRC-B", name_ar="موقع مصدر ب",
        )
        destination_location = StorageLocation.objects.create(
            warehouse=destination_warehouse, code="DST-A", name_ar="موقع وجهة أ",
        )
        expiry_a = date(2027, 1, 31)
        expiry_b = date(2027, 2, 28)

        for number, location, expiry, quantity, cost in (
            ("LOT-R-A", source_location_a, expiry_a, "5", "10"),
            ("LOT-R-B", source_location_b, expiry_b, "4", "20"),
        ):
            receipt = self._movement(
                number, StockMovement.MovementType.RECEIPT,
                destination=self.warehouse, quantity=quantity, unit_cost=cost,
                destination_location=location, expiry_date=expiry,
            )
            post_stock_movement(receipt.pk, self.user)

        clearing = self._account(
            "2191", "تسوية تحويل مخزون", ChartOfAccount.Nature.LIABILITY,
        )
        AccountRole.objects.create(
            company=self.company, role=AccountRole.Role.INTER_BRANCH_CLEARING,
            account=clearing,
        )
        transfer = self._movement(
            "LOT-TRANSFER-01", StockMovement.MovementType.TRANSFER,
            source=self.warehouse, destination=destination_warehouse,
            quantity="3", source_location=source_location_a,
            destination_location=destination_location, expiry_date=expiry_a,
        )
        post_stock_movement(transfer.pk, self.user)

        source_a = StockBalance.objects.get(
            product=self.product, warehouse=self.warehouse,
            location=source_location_a, expiry_date=expiry_a,
        )
        source_b = StockBalance.objects.get(
            product=self.product, warehouse=self.warehouse,
            location=source_location_b, expiry_date=expiry_b,
        )
        destination_a = StockBalance.objects.get(
            product=self.product, warehouse=destination_warehouse,
            location=destination_location, expiry_date=expiry_a,
        )
        self.assertEqual(
            (source_a.quantity, source_a.inventory_value),
            (Decimal("2"), Decimal("20.0000")),
        )
        self.assertEqual(
            (source_b.quantity, source_b.inventory_value),
            (Decimal("4"), Decimal("80.0000")),
        )
        self.assertEqual(
            (destination_a.quantity, destination_a.inventory_value),
            (Decimal("3"), Decimal("30.0000")),
        )
        self.assertEqual(transfer.lines.get().cost_amount, Decimal("30.0000"))
        self.assertEqual(
            list(self.product.cost_layers.filter(
                warehouse=self.warehouse, location=source_location_a, expiry_date=expiry_a,
            ).values_list("quantity_remaining", flat=True)),
            [Decimal("2.0000")],
        )
        self.assertEqual(
            list(self.product.cost_layers.filter(
                warehouse=destination_warehouse, location=destination_location,
                expiry_date=expiry_a,
            ).values_list("quantity_remaining", flat=True)),
            [Decimal("3.0000")],
        )
        transfer_entries = list(
            transfer.journal_links.select_related("journal_entry").all()
        )
        self.assertEqual(len(transfer_entries), 2)
        self.assertTrue(all(
            link.journal_entry.status == JournalEntry.Status.POSTED
            for link in transfer_entries
        ))
        self.assertEqual(
            sum(
                (link.journal_entry.lines.filter(account=clearing).aggregate(
                    debit=Sum("debit"), credit=Sum("credit"),
                )["debit"] or Decimal("0")) -
                (link.journal_entry.lines.filter(account=clearing).aggregate(
                    debit=Sum("debit"), credit=Sum("credit"),
                )["credit"] or Decimal("0"))
                for link in transfer_entries
            ),
            Decimal("0.0000"),
        )

    def test_incomplete_fifo_layers_roll_back_partial_consumption(self):
        self.product.costing_method = Product.CostingMethod.FIFO
        self.product.save(update_fields=("costing_method",))
        receipt = self._movement(
            "FIFO-ROLLBACK-R", StockMovement.MovementType.RECEIPT,
            destination=self.warehouse, quantity="2", unit_cost="7",
        )
        post_stock_movement(receipt.pk, self.user)
        layer = StockCostLayer.objects.get(product=self.product, warehouse=self.warehouse)
        layer.quantity_remaining = Decimal("1")
        layer.save(update_fields=("quantity_remaining",))

        issue = self._movement(
            "FIFO-ROLLBACK-I", StockMovement.MovementType.ISSUE,
            source=self.warehouse, quantity="2",
        )
        with self.assertRaises(ValidationError):
            post_stock_movement(issue.pk, self.user)

        layer.refresh_from_db()
        balance = StockBalance.objects.get(product=self.product, warehouse=self.warehouse)
        issue.refresh_from_db()
        self.assertEqual(layer.quantity_remaining, Decimal("1.0000"))
        self.assertEqual(balance.quantity, Decimal("2.0000"))
        self.assertEqual(balance.inventory_value, Decimal("14.0000"))
        self.assertEqual(issue.status, StockMovement.Status.DRAFT)
        self.assertFalse(issue.journal_links.exists())


    def _create_inventory_journal(self, branch, number, debit, *, status=JournalEntry.Status.POSTED):
        entry = JournalEntry.objects.create(
            company=self.company, branch=branch, fiscal_year=self.year,
            entry_number=number, entry_date=date(2026, 2, 1),
            description="اختبار مطابقة المخزون", source_type=JournalEntry.Source.INVENTORY,
            created_by=self.user,
        )
        JournalLine.objects.create(
            entry=entry, account=self.inventory_account, debit=Decimal(debit), credit=Decimal("0"),
        )
        JournalLine.objects.create(
            entry=entry, account=self.grni_account, debit=Decimal("0"), credit=Decimal(debit),
        )
        if status != JournalEntry.Status.DRAFT:
            JournalEntry.objects.filter(pk=entry.pk).update(status=status)
        return entry

    def test_fifo_integrity_keeps_location_and_expiry_dimensions_and_ignores_draft_layers(self):
        self.product.costing_method = Product.CostingMethod.FIFO
        self.product.track_expiry = True
        self.product.save(update_fields=("costing_method", "track_expiry"))
        location_a = StorageLocation.objects.create(
            warehouse=self.warehouse, code="A-01", name_ar="الموقع الأول",
        )
        location_b = StorageLocation.objects.create(
            warehouse=self.warehouse, code="B-01", name_ar="الموقع الثاني",
        )
        expiry_a = date(2027, 1, 31)
        expiry_b = date(2027, 2, 28)

        StockBalance.objects.create(
            product=self.product, warehouse=self.warehouse, location=location_a,
            expiry_date=expiry_a, quantity=Decimal("5"), inventory_value=Decimal("50"),
            average_cost=Decimal("10"),
        )

        posted_receipt = StockMovement.objects.create(
            company=self.company, movement_number="FIFO-INTEGRITY-POSTED",
            movement_type=StockMovement.MovementType.RECEIPT,
            movement_date=date(2026, 2, 1), destination_warehouse=self.warehouse,
            created_by=self.user, status=StockMovement.Status.POSTED,
        )
        posted_line = StockMovementLine.objects.create(
            movement=posted_receipt, product=self.product,
            destination_location=location_b, expiry_date=expiry_b,
            quantity=Decimal("5"), unit_cost=Decimal("10"), cost_amount=Decimal("50"),
        )
        StockCostLayer.objects.create(
            product=self.product, warehouse=self.warehouse, source_line=posted_line,
            location=location_b, expiry_date=expiry_b,
            quantity_remaining=Decimal("5"), unit_cost=Decimal("10"),
        )

        draft_receipt = StockMovement.objects.create(
            company=self.company, movement_number="FIFO-INTEGRITY-DRAFT",
            movement_type=StockMovement.MovementType.RECEIPT,
            movement_date=date(2026, 2, 2), destination_warehouse=self.warehouse,
            created_by=self.user, status=StockMovement.Status.DRAFT,
        )
        draft_line = StockMovementLine.objects.create(
            movement=draft_receipt, product=self.product,
            destination_location=location_a, expiry_date=expiry_a,
            quantity=Decimal("5"), unit_cost=Decimal("10"), cost_amount=Decimal("50"),
        )
        # A stray layer linked to a draft movement must never enter the integrity totals.
        StockCostLayer.objects.create(
            product=self.product, warehouse=self.warehouse, source_line=draft_line,
            location=location_a, expiry_date=expiry_a,
            quantity_remaining=Decimal("5"), unit_cost=Decimal("10"),
        )
        CompanyMembership.objects.create(
            user=self.user, company=self.company,
            role=CompanyMembership.Role.VIEWER, all_branches=True,
        )

        request = APIRequestFactory().get(
            "/api/inventory/balances/valuation-integrity/",
            {"company": self.company.pk},
        )
        force_authenticate(request, user=self.user)
        response = StockBalanceViewSet.as_view({"get": "valuation_integrity"})(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["fifo_mismatch_count"], 2)
        mismatch_keys = {
            (row["location"], row["expiry_date"])
            for row in response.data["fifo_mismatches"]
        }
        self.assertEqual(
            mismatch_keys,
            {(location_a.pk, expiry_a.isoformat()), (location_b.pk, expiry_b.isoformat())},
        )
        # Draft movements are reported as pending work, not counted as posted FIFO layers.
        self.assertEqual(response.data["pending_movements_count"], 1)
        self.assertEqual(
            response.data["pending_movements"][0]["movement_number"],
            "FIFO-INTEGRITY-DRAFT",
        )

    def test_reconciliation_matches_stock_to_posted_gl_for_authorized_branch_only(self):
        second_branch = Branch.objects.create(
            company=self.company, code="BR-REC", name_ar="فرع المطابقة",
        )
        second_warehouse = Warehouse.objects.create(
            company=self.company, branch=second_branch,
            code="WH-REC", name_ar="مخزن المطابقة",
        )
        StockBalance.objects.create(
            product=self.product, warehouse=self.warehouse,
            quantity=Decimal("5"), inventory_value=Decimal("25"),
            average_cost=Decimal("5"),
        )
        StockBalance.objects.create(
            product=self.product, warehouse=second_warehouse,
            quantity=Decimal("7"), inventory_value=Decimal("70"),
            average_cost=Decimal("10"),
        )
        self._create_inventory_journal(self.branch, "REC-HQ", "25")
        self._create_inventory_journal(second_branch, "REC-BR", "70")
        # Draft entries must not affect the posted general-ledger balance.
        self._create_inventory_journal(
            self.branch, "REC-DRAFT", "100", status=JournalEntry.Status.DRAFT,
        )
        membership = CompanyMembership.objects.create(
            user=self.user, company=self.company,
            role=CompanyMembership.Role.VIEWER, all_branches=False,
        )
        membership.branches.add(second_branch)

        request = APIRequestFactory().get(
            "/api/inventory/balances/reconciliation/",
            {"company": self.company.pk, "branch": second_branch.pk},
        )
        force_authenticate(request, user=self.user)
        response = StockBalanceViewSet.as_view({"get": "reconciliation"})(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Decimal(response.data["stock_value"]), Decimal("70.0000"))
        self.assertEqual(Decimal(response.data["gl_balance"]), Decimal("70.0000"))
        self.assertEqual(Decimal(response.data["difference"]), Decimal("0.0000"))
        self.assertTrue(response.data["is_reconciled"])
        self.assertEqual(response.data["scope_branch_ids"], [second_branch.pk])

    def test_reconciliation_rejects_branch_outside_membership(self):
        second_branch = Branch.objects.create(
            company=self.company, code="BR-DENY", name_ar="فرع غير مصرح",
        )
        CompanyMembership.objects.create(
            user=self.user, company=self.company,
            role=CompanyMembership.Role.VIEWER, all_branches=False,
        )
        request = APIRequestFactory().get(
            "/api/inventory/balances/reconciliation/",
            {"company": self.company.pk, "branch": second_branch.pk},
        )
        force_authenticate(request, user=self.user)
        response = StockBalanceViewSet.as_view({"get": "reconciliation"})(request)

        self.assertEqual(response.status_code, 403)
