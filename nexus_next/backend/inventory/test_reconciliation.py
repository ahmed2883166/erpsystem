from datetime import date
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from accounting.models import AccountRole, ChartOfAccount, JournalEntry, JournalLine
from accounting.services import post_journal
from companies.models import Branch, Company, CompanyMembership, Currency, FiscalYear
from .models import Product, StockBalance, UnitOfMeasure, Warehouse


class InventoryReconciliationBranchScopeTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        currency = Currency.objects.create(code="EGP", name_ar="جنيه مصري")
        cls.company = Company.objects.create(
            code="INV-RECON", name_ar="شركة مطابقة المخزون", country="EG",
            activity="commercial", base_currency=currency,
        )
        cls.head_office = Branch.objects.create(
            company=cls.company, code="HQ", name_ar="المركز الرئيسي", is_head_office=True,
        )
        cls.second_branch = Branch.objects.create(
            company=cls.company, code="BR2", name_ar="الفرع الثاني",
        )
        cls.archived_branch = Branch.objects.create(
            company=cls.company, code="OLD", name_ar="فرع مؤرشف", is_active=False,
        )
        cls.year = FiscalYear.objects.create(
            company=cls.company, name="2026",
            start_date=date(2026, 1, 1), end_date=date(2026, 12, 31),
        )
        cls.inventory_account = ChartOfAccount.objects.create(
            company=cls.company, code="1200", name_ar="المخزون", nature="asset",
        )
        offset = ChartOfAccount.objects.create(
            company=cls.company, code="3000", name_ar="حساب مقابل", nature="equity",
        )
        AccountRole.objects.create(
            company=cls.company, role=AccountRole.Role.INVENTORY, account=cls.inventory_account,
        )
        unit = UnitOfMeasure.objects.create(
            company=cls.company, code="EA", name_ar="قطعة",
        )
        product = Product.objects.create(
            company=cls.company, unit=unit, code="P-1", name_ar="صنف اختبار",
        )
        head_warehouse = Warehouse.objects.create(
            company=cls.company, branch=cls.head_office, code="WH-HQ", name_ar="مخزن الرئيسي",
        )
        unassigned_warehouse = Warehouse.objects.create(
            company=cls.company, branch=None, code="WH-DEFAULT", name_ar="مخزن غير مربوط بفرع",
        )
        second_warehouse = Warehouse.objects.create(
            company=cls.company, branch=cls.second_branch, code="WH-2", name_ar="مخزن الفرع الثاني",
        )
        archived_warehouse = Warehouse.objects.create(
            company=cls.company, branch=cls.archived_branch, code="WH-OLD", name_ar="مخزن الفرع المؤرشف",
        )
        StockBalance.objects.create(
            product=product, warehouse=head_warehouse, quantity=Decimal("10"),
            inventory_value=Decimal("100"), average_cost=Decimal("10"),
        )
        StockBalance.objects.create(
            product=product, warehouse=unassigned_warehouse, quantity=Decimal("2"),
            inventory_value=Decimal("20"), average_cost=Decimal("10"),
        )
        StockBalance.objects.create(
            product=product, warehouse=second_warehouse, quantity=Decimal("5"),
            inventory_value=Decimal("50.0000"), average_cost=Decimal("10"),
        )
        StockBalance.objects.create(
            product=product, warehouse=archived_warehouse, quantity=Decimal("1"),
            inventory_value=Decimal("5.0000"), average_cost=Decimal("5"),
        )
        cls._post_inventory_balance("RECON-HQ", cls.head_office, offset, Decimal("120.0000"))
        cls._post_inventory_balance("RECON-BR2", cls.second_branch, offset, Decimal("50.0000"))
        cls._post_inventory_balance("RECON-OLD", cls.archived_branch, offset, Decimal("5.0000"))

        cls.full_user = User.objects.create_user(username="inventory_full", password="test-password")
        CompanyMembership.objects.create(
            user=cls.full_user, company=cls.company, role="viewer", all_branches=True,
        )
        cls.limited_user = User.objects.create_user(username="inventory_limited", password="test-password")
        membership = CompanyMembership.objects.create(
            user=cls.limited_user, company=cls.company, role="viewer", all_branches=False,
        )
        membership.branches.add(cls.head_office)

    @classmethod
    def _post_inventory_balance(cls, number, branch, offset, amount):
        entry = JournalEntry.objects.create(
            company=cls.company, branch=branch, fiscal_year=cls.year,
            entry_number=number, entry_date=date(2026, 2, 1), description="رصيد مخزون للاختبار",
        )
        JournalLine.objects.create(
            entry=entry, account=cls.inventory_account, debit=amount, credit=0,
        )
        JournalLine.objects.create(
            entry=entry, account=offset, debit=0, credit=amount,
        )
        post_journal(entry.pk)

    def test_all_accessible_branches_reconcile_and_unassigned_warehouse_maps_to_head_office(self):
        self.client.force_authenticate(user=self.full_user)
        response = self.client.get(f"/api/inventory/balances/reconciliation/?company={self.company.pk}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["stock_value"], "175.0000")
        self.assertEqual(response.data["gl_balance"], "175.0000")
        self.assertEqual(response.data["difference"], "0.0000")
        self.assertCountEqual(
            response.data["scope_branch_ids"], [self.head_office.pk, self.second_branch.pk, self.archived_branch.pk],
        )

    def test_branch_filter_reconciles_only_the_selected_branch(self):
        self.client.force_authenticate(user=self.full_user)
        head = self.client.get(
            f"/api/inventory/balances/reconciliation/?company={self.company.pk}&branch={self.head_office.pk}"
        )
        second = self.client.get(
            f"/api/inventory/balances/reconciliation/?company={self.company.pk}&branch={self.second_branch.pk}"
        )
        self.assertEqual(head.status_code, 200)
        self.assertEqual(head.data["stock_value"], "120.0000")
        self.assertEqual(head.data["gl_balance"], "120.0000")
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.data["stock_value"], "50.0000")
        self.assertEqual(second.data["gl_balance"], "50.0000")

    def test_fifo_integrity_report_detects_layer_mismatch_and_pending_movements(self):
        from .models import StockCostLayer, StockMovement, StockMovementLine

        product = Product.objects.create(
            company=self.company, unit=UnitOfMeasure.objects.get(company=self.company),
            code="FIFO-1", name_ar="صنف FIFO", costing_method=Product.CostingMethod.FIFO,
        )
        warehouse = Warehouse.objects.get(company=self.company, code="WH-HQ")
        balance = StockBalance.objects.create(
            product=product, warehouse=warehouse, quantity=Decimal("10"),
            inventory_value=Decimal("100"), average_cost=Decimal("10"),
        )
        movement = StockMovement.objects.create(
            company=self.company, movement_number="DRAFT-FIFO", movement_type=StockMovement.MovementType.RECEIPT,
            movement_date=date(2026, 3, 1), destination_warehouse=warehouse,
            created_by=self.full_user,
        )
        line = StockMovementLine.objects.create(
            movement=movement, product=product, quantity=Decimal("10"),
            unit_cost=Decimal("10"), cost_amount=Decimal("100"),
        )
        StockCostLayer.objects.create(
            product=product, warehouse=warehouse, source_line=line,
            quantity_remaining=Decimal("9"), unit_cost=Decimal("10"),
        )
        self.client.force_authenticate(user=self.full_user)
        response = self.client.get(f"/api/inventory/balances/valuation-integrity/?company={self.company.pk}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["fifo_mismatch_count"], 1)
        self.assertEqual(response.data["fifo_mismatches"][0]["quantity_difference"], "1.0000")
        self.assertEqual(response.data["fifo_mismatches"][0]["value_difference"], "10.0000")
        self.assertEqual(response.data["pending_movements_count"], 1)
        self.assertEqual(response.data["pending_movements"][0]["movement_number"], "DRAFT-FIFO")

    def test_pending_cross_branch_movement_is_not_exposed_to_limited_branch_user(self):
        from .models import StockMovement

        StockMovement.objects.create(
            company=self.company, movement_number="DRAFT-CROSS",
            movement_type=StockMovement.MovementType.TRANSFER,
            movement_date=date(2026, 3, 2),
            source_warehouse=Warehouse.objects.get(company=self.company, code="WH-HQ"),
            destination_warehouse=Warehouse.objects.get(company=self.company, code="WH-2"),
            created_by=self.limited_user,
        )
        self.client.force_authenticate(user=self.limited_user)
        response = self.client.get(f"/api/inventory/balances/valuation-integrity/?company={self.company.pk}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["pending_movements_count"], 0)

    def test_limited_user_sees_only_accessible_branches_and_cannot_query_another_branch(self):
        self.client.force_authenticate(user=self.limited_user)
        scoped = self.client.get(f"/api/inventory/balances/reconciliation/?company={self.company.pk}")
        denied = self.client.get(
            f"/api/inventory/balances/reconciliation/?company={self.company.pk}&branch={self.second_branch.pk}"
        )
        self.assertEqual(scoped.status_code, 200)
        self.assertEqual(scoped.data["stock_value"], "120.0000")
        self.assertEqual(scoped.data["gl_balance"], "120.0000")
        self.assertEqual(denied.status_code, 403)
