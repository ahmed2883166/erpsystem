"""End-to-end fixed-asset lifecycle checks across the financial statements."""

import sqlite3
import unittest
from decimal import Decimal
from unittest.mock import patch

from accounting_core import register_decimal_aggregates
from fixed_assets import _asset_disposal_journal_lines, _asset_period_amount
from income_statement import build_income_statement
from financial_planning import (
    build_balance_sheet_snapshot,
    build_cash_flow_indirect_snapshot,
    build_cash_flow_snapshot,
)


class FixedAssetLifecycleFinancialReportTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        register_decimal_aggregates(self.conn)
        self.conn.executescript("""
            CREATE TABLE accounts(
                id INTEGER PRIMARY KEY, code TEXT, name_ar TEXT,
                account_type TEXT, parent_id INTEGER,
                is_group INTEGER DEFAULT 0, active INTEGER DEFAULT 1,
                statement_section TEXT, account_subtype TEXT
            );
            CREATE TABLE journal_entries(
                id INTEGER PRIMARY KEY, entry_date TEXT, status TEXT,
                entry_type TEXT DEFAULT 'general', reference TEXT
            );
            CREATE TABLE journal_lines(
                id INTEGER PRIMARY KEY, entry_id INTEGER, account_id INTEGER,
                debit TEXT, credit TEXT
            );
            CREATE TABLE management_account_roles(account_id INTEGER, role TEXT);
            CREATE TABLE management_dashboard_meta(meta_key TEXT PRIMARY KEY, meta_value TEXT);
            CREATE TABLE cash_flow_account_mappings(
                account_id INTEGER PRIMARY KEY, category TEXT NOT NULL
            );
            CREATE TABLE fixed_assets(
                id INTEGER PRIMARY KEY, expense_account_id INTEGER
            );
            CREATE TABLE asset_depreciations(
                id INTEGER PRIMARY KEY, asset_id INTEGER, period TEXT,
                amount TEXT, journal_entry_id INTEGER
            );
            CREATE TABLE asset_disposals(
                id INTEGER PRIMARY KEY, asset_id INTEGER, disposal_date TEXT,
                gain_loss_account_id INTEGER, gain_loss_amount TEXT,
                journal_entry_id INTEGER
            );
            INSERT INTO accounts VALUES
                (1,'1110','البنك','asset',NULL,0,1,'current_assets',NULL),
                (2,'1210','تكلفة الأصول الثابتة','asset',NULL,0,1,'non_current_assets',NULL),
                (3,'1290','مجمع إهلاك الأصول','asset',NULL,0,1,'non_current_assets',
                    'accumulated_depreciation_or_amortization'),
                (4,'6300','مصروف إهلاك الأصول','expense',NULL,0,1,'operating_expenses',NULL),
                (5,'4710','أرباح وخسائر استبعاد الأصول','revenue',NULL,0,1,'other_income',NULL),
                (6,'3100','رأس المال','equity',NULL,0,1,'equity',NULL);
            INSERT INTO management_account_roles VALUES(1,'cash');
            INSERT INTO fixed_assets VALUES(1,4),(2,4);
        """)
        self.conn.execute(
            "INSERT INTO journal_entries(id,entry_date,status,entry_type,reference) "
            "VALUES(1,'2025-12-31','posted','general','OPENING-CASH')"
        )
        self._insert_lines(1, [(1, "10000", "0"), (6, "0", "10000")])
        self.next_entry_id = 2
        self.next_line_id = 3
        self.next_depreciation_id = 1
        self.next_disposal_id = 1

    def tearDown(self):
        self.conn.close()

    def _post_entry(self, entry_date, reference, lines):
        entry_id = self.next_entry_id
        self.next_entry_id += 1
        self.conn.execute(
            "INSERT INTO journal_entries(id,entry_date,status,entry_type,reference) "
            "VALUES(?,?, 'posted','general',?)",
            (entry_id, entry_date, reference),
        )
        self._insert_lines(entry_id, lines)
        return entry_id

    def _insert_lines(self, entry_id, lines):
        for account_id, debit, credit in lines:
            self.conn.execute(
                "INSERT INTO journal_lines(id,entry_id,account_id,debit,credit) "
                "VALUES(?,?,?,?,?)",
                (self.next_line_id, entry_id, account_id, str(debit), str(credit)),
            )
            self.next_line_id += 1

    def _asset(self, asset_id, name):
        return {
            "id": asset_id,
            "asset_code": f"FA-{asset_id:03d}",
            "asset_name": name,
            "asset_account_id": 2,
            "accum_account_id": 3,
            "expense_account_id": 4,
            "ready_date": "2026-01-01",
            "cost": "1200",
            "residual_value": "0",
            "useful_life_months": 12,
            "depreciation_method": "straight_line",
            "annual_rate": "0",
        }

    def test_purchase_three_months_depreciation_and_gain_and_loss_sales_reconcile(self):
        assets = [self._asset(1, "سيارة تحقق ربح"), self._asset(2, "سيارة تحقق خسارة")]

        # Capitalize both purchases and pay cash.
        for asset in assets:
            self._post_entry(
                "2026-01-02", f"ASSET-PURCHASE-{asset['id']}",
                [(2, "1200", "0"), (1, "0", "1200")],
            )

        # Post three consecutive monthly depreciation periods for each asset.
        period_ends = {
            "2026-01": "2026-01-31",
            "2026-02": "2026-02-28",
            "2026-03": "2026-03-31",
        }
        for asset in assets:
            accumulated = Decimal("0")
            for period, period_end in period_ends.items():
                amount = _asset_period_amount(asset, accumulated, period)
                self.assertEqual(amount, Decimal("100.00"))
                entry_id = self._post_entry(
                    period_end, f"ASSET-DEPRECIATION-{asset['id']}-{period}",
                    [(4, amount, "0"), (3, "0", amount)],
                )
                self.conn.execute(
                    "INSERT INTO asset_depreciations(id,asset_id,period,amount,journal_entry_id) "
                    "VALUES(?,?,?,?,?)",
                    (self.next_depreciation_id, asset["id"], period, str(amount), entry_id),
                )
                self.next_depreciation_id += 1
                accumulated += amount
            self.assertEqual(accumulated, Decimal("300.00"))

        # Sell one asset above NBV (gain 100) and the other below NBV (loss 200).
        disposals = [
            (assets[0], Decimal("1000"), Decimal("100")),
            (assets[1], Decimal("700"), Decimal("-200")),
        ]
        for asset, proceeds, gain_loss in disposals:
            accumulated = Decimal("300")
            net_book_value = Decimal(asset["cost"]) - accumulated
            self.assertEqual(net_book_value, Decimal("900"))
            lines = _asset_disposal_journal_lines(
                asset, Decimal(asset["cost"]), accumulated, proceeds, Decimal("0"),
                1, None, 5, gain_loss,
            )
            self.assertEqual(
                sum((Decimal(str(line[1])) for line in lines), Decimal("0")),
                sum((Decimal(str(line[2])) for line in lines), Decimal("0")),
            )
            entry_id = self._post_entry(
                "2026-03-31", f"ASSET-DISPOSAL-{asset['id']}",
                [(line[0], line[1], line[2]) for line in lines],
            )
            self.conn.execute(
                "INSERT INTO asset_disposals"
                "(id,asset_id,disposal_date,gain_loss_account_id,gain_loss_amount,journal_entry_id) "
                "VALUES(?,?,?,?,?,?)",
                (self.next_disposal_id, asset["id"], "2026-03-31", 5, str(gain_loss), entry_id),
            )
            self.next_disposal_id += 1

        self.conn.commit()

        income = build_income_statement(self.conn, "2026-01-01", "2026-03-31")
        balance = build_balance_sheet_snapshot(self.conn, "2026-03-31")
        with (
            patch("activity_profiles.get_active_activity_profile",
                  return_value={"code": "commercial", "base_activity": "commercial"}),
            patch("activity_profiles.active_account_role_ids", return_value={}),
        ):
            direct_cash = build_cash_flow_snapshot(self.conn, "2026-01-01", "2026-03-31")
            indirect_cash = build_cash_flow_indirect_snapshot(
                self.conn, "2026-01-01", "2026-03-31"
            )

        # Income statement separates gain and loss despite their shared revenue-type account.
        self.assertEqual(income["revenue_total"], Decimal("100"))
        self.assertEqual(income["operating_expense_total"], Decimal("800"))
        self.assertEqual(income["net_profit"], Decimal("-700"))
        self.assertTrue(any(
            row["name_ar"].startswith("ربح استبعاد أصل")
            and row["balance"] == Decimal("100")
            for row in income["revenue_rows"]
        ))
        self.assertTrue(any(
            row["name_ar"].startswith("خسارة استبعاد أصل")
            and row["balance"] == Decimal("200")
            for row in income["operating_expense_rows"]
        ))

        # Both assets are fully removed: cost and accumulated depreciation clear to zero.
        self.assertEqual(balance["total_assets"], Decimal("9300"))
        self.assertEqual(balance["total_liabilities"], Decimal("0"))
        self.assertEqual(balance["net_profit"], Decimal("-700"))
        self.assertEqual(balance["total_equity"], Decimal("9300"))
        self.assertTrue(balance["balanced"])
        self.assertEqual(balance["negative_asset_accounts"], [])

        # Asset purchases and sale proceeds are investing cash flows; depreciation is non-cash.
        self.assertEqual(direct_cash["opening"], Decimal("10000"))
        self.assertEqual(direct_cash["closing"], Decimal("9300"))
        self.assertEqual(direct_cash["sections"]["investing"]["out"], Decimal("2400"))
        self.assertEqual(direct_cash["sections"]["investing"]["in"], Decimal("1700"))
        self.assertEqual(direct_cash["reconciliation_difference"], Decimal("0"))
        self.assertEqual(indirect_cash["indirect_operating_net"], Decimal("0"))
        self.assertEqual(indirect_cash["sections"]["investing"]["out"], Decimal("2400"))
        self.assertEqual(indirect_cash["sections"]["investing"]["in"], Decimal("1700"))
        self.assertEqual(indirect_cash["reconciliation_difference"], Decimal("0"))

        # Every journal in the lifecycle remains balanced.
        totals = self.conn.execute(
            "SELECT DECIMAL_SUM(debit) debit, DECIMAL_SUM(credit) credit FROM journal_lines"
        ).fetchone()
        self.assertEqual(Decimal(str(totals["debit"])), Decimal(str(totals["credit"])))
        self.assertEqual(Decimal(str(totals["debit"])), Decimal("15500"))


if __name__ == "__main__":
    unittest.main()
