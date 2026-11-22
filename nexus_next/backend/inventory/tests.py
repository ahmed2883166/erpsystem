from datetime import date
from decimal import Decimal

from django.db.models import Sum

from django.test import TestCase
from rest_framework.exceptions import ValidationError

from accounts.models import User
from accounting.models import AccountRole, ChartOfAccount, JournalEntry
from companies.models import Branch, Company, Currency, FiscalYear

from .models import (
    Product, StockBalance, StockCostLayer, StockMovement, StockMovementLine, UnitOfMeasure, Warehouse,
)
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

    def _movement(self, number, kind, *, source=None, destination=None, quantity="1", unit_cost="0", direction=""):
        movement = StockMovement.objects.create(
            company=self.company, movement_number=number, movement_type=kind,
            movement_date=date(2026, 2, 1), source_warehouse=source,
            destination_warehouse=destination, created_by=self.user,
        )
        StockMovementLine.objects.create(
            movement=movement, product=self.product, quantity=Decimal(quantity),
            unit_cost=Decimal(unit_cost), adjustment_direction=direction,
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
