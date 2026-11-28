import os
import time
import unittest
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIClient

from companies.models import Branch, Company, CompanyMembership, Currency
from inventory.models import Product, StockMovement, StockMovementLine, UnitOfMeasure, Warehouse


@unittest.skipUnless(
    os.getenv("RUN_INVENTORY_500K_BENCHMARK") == "1",
    "Set RUN_INVENTORY_500K_BENCHMARK=1 to run the 500k inventory benchmark.",
)
class Inventory500KMovementPerformanceTests(TransactionTestCase):
    reset_sequences = True

    def test_stock_card_paginates_half_million_posted_movements(self):
        currency = Currency.objects.create(code="EGP", name_ar="جنيه مصري")
        company = Company.objects.create(
            code="INV500K", name_ar="شركة اختبار أداء المخزون",
            country="EG", activity="commercial", base_currency=currency,
        )
        branch = Branch.objects.create(
            company=company, code="B500K", name_ar="فرع اختبار الأداء",
            is_head_office=True,
        )
        warehouse = Warehouse.objects.create(
            company=company, branch=branch, code="WH500K", name_ar="مخزن اختبار الأداء",
        )
        unit = UnitOfMeasure.objects.create(
            company=company, code="PCS", name_ar="قطعة",
        )
        product = Product.objects.create(
            company=company, unit=unit, code="P500K", name_ar="صنف اختبار 500 ألف",
            costing_method=Product.CostingMethod.FIFO,
        )
        user = get_user_model().objects.create_user(
            username="inventory-500k-benchmark", password="benchmark-only-password",
        )
        CompanyMembership.objects.create(
            user=user, company=company, role="admin", all_branches=True,
        )

        now = timezone.now()
        total = 500_000
        batch_size = 5_000
        for start in range(0, total, batch_size):
            stop = min(start + batch_size, total)
            movements = StockMovement.objects.bulk_create([
                StockMovement(
                    company=company,
                    movement_number=f"BENCH-{index:06d}",
                    movement_type=StockMovement.MovementType.RECEIPT,
                    movement_date=date(2026, 1, 1),
                    destination_warehouse=warehouse,
                    status=StockMovement.Status.POSTED,
                    created_by=user,
                    posted_by=user,
                    posted_at=now,
                )
                for index in range(start, stop)
            ], batch_size=batch_size)
            StockMovementLine.objects.bulk_create([
                StockMovementLine(
                    movement=movement,
                    product=product,
                    quantity=Decimal("1.0000"),
                    unit_cost=Decimal("2.0000"),
                    cost_amount=Decimal("2.0000"),
                )
                for movement in movements
            ], batch_size=batch_size)

        client = APIClient()
        client.force_authenticate(user)
        started = time.perf_counter()
        response = client.get(
            "/api/inventory/movements/stock-card/",
            {"product": product.pk, "warehouse": warehouse.pk, "page": 1, "page_size": 50},
        )
        elapsed = time.perf_counter() - started

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["pagination"]["total_rows"], total)
        self.assertEqual(len(response.data["rows"]), 50)
        self.assertLess(elapsed, 60, f"Stock card took {elapsed:.2f}s for 500,000 movement lines.")
