import os
import json
from decimal import Decimal
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

os.environ.setdefault("NEXUS_SECRET_KEY", "unit-test-secret-key-which-is-long-enough-123")
os.environ["NEXUS_SKIP_DB_INIT"] = "1"

import app as nexus
import stock
from activity_profiles import initialize_activity_profiles_schema, create_activity_profile


class StocktakeRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_stock_path = stock.DB_PATH
        stock.DB_PATH = Path(self.temp.name) / "stocktake.db"
        stock.initialize_stock_schema()
        conn = stock.get_db()
        conn.execute("INSERT INTO stock_warehouses(code,name_ar) VALUES('W1','المخزن الرئيسي')")
        self.warehouse_id = conn.execute("SELECT id FROM stock_warehouses").fetchone()["id"]
        conn.execute("INSERT INTO stock_products(sku,name_ar) VALUES('P1','صنف تجريبي')")
        self.product_id = conn.execute("SELECT id FROM stock_products").fetchone()["id"]
        conn.execute("""INSERT INTO stock_movements(movement_no,movement_type,product_id,target_warehouse_id,
            quantity,unit_cost,total_cost,movement_date) VALUES('OPEN-1','receipt',?,?, '10','5','50','2026-01-01')""",
            (self.product_id,self.warehouse_id))
        movement_id = conn.execute("SELECT id FROM stock_movements WHERE movement_no='OPEN-1'").fetchone()["id"]
        conn.execute("""INSERT INTO stock_valuation_layers(product_id,warehouse_id,source_movement_id,
            qty_in,qty_remaining,unit_cost,value_remaining) VALUES(?,?,?,'10','10','5','50')""",
            (self.product_id,self.warehouse_id,movement_id))
        conn.execute("CREATE TABLE accounts(id INTEGER PRIMARY KEY,code TEXT,name_ar TEXT,account_type TEXT,is_group INTEGER,active INTEGER)")
        conn.executemany("INSERT INTO accounts(code,name_ar,account_type,is_group,active) VALUES(?,?,?,?,?)", [
            ("1130","مخزون","asset",0,1),
            ("6990","فروق الجرد","expense",0,1),
        ])
        conn.execute("""CREATE TABLE financial_years(
            id INTEGER PRIMARY KEY, year_name TEXT, start_date TEXT, end_date TEXT, status TEXT
        )""")
        conn.execute("INSERT INTO financial_years VALUES(1,'2025','2025-01-01','2025-12-31','closed')")
        conn.execute("INSERT INTO financial_years VALUES(2,'2026','2026-01-01','2026-12-31','open')")
        conn.execute("""CREATE TABLE accounting_periods(
            id INTEGER PRIMARY KEY, period_name TEXT, start_date TEXT, end_date TEXT, locked INTEGER
        )""")
        conn.commit()
        conn.close()
        nexus.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        self.client = nexus.app.test_client()

    def tearDown(self):
        stock.DB_PATH = self.old_stock_path
        self.temp.cleanup()

    def _login(self):
        with self.client.session_transaction() as session:
            session.update(user_id=7, username="stockadmin", full_name="مدير المخزن",
                           role="admin", permissions=["stock.manage"])

    def test_stocktake_captures_book_snapshot_and_saves_actual_variance(self):
        self._login()
        response = self.client.post("/stock/counts", data={
            "warehouse_id": str(self.warehouse_id), "count_date": "2026-11-11",
            "notes": "جرد اختباري",
        })
        self.assertEqual(response.status_code, 302)
        conn = stock.get_db()
        header = conn.execute("SELECT * FROM stocktakes").fetchone()
        line = conn.execute("SELECT * FROM stocktake_lines").fetchone()
        self.assertEqual(header["status"], "draft")
        self.assertEqual(line["snapshot_qty"], "10.0000")
        detail_response = self.client.get("/stock/counts/" + str(header["id"]))
        self.assertEqual(detail_response.status_code, 200)
        response = self.client.post("/stock/counts/" + str(header["id"]), data={
            "counted_" + str(line["id"]): "8", "cost_" + str(line["id"]): "5",
        })
        self.assertEqual(response.status_code, 302)
        updated = conn.execute("SELECT * FROM stocktake_lines WHERE id=?", (line["id"],)).fetchone()
        status = conn.execute("SELECT status FROM stocktakes WHERE id=?", (header["id"],)).fetchone()["status"]
        self.assertEqual(updated["counted_qty"], "8.0000")
        self.assertEqual(updated["variance_qty"], "-2.0000")
        self.assertEqual(status, "counted")
        conn.close()



    def test_stocktake_creates_separate_lines_per_bin_and_lot(self):
        self._login()
        conn = stock.get_db()
        conn.execute("UPDATE stock_products SET track_lot=1 WHERE id=?", (self.product_id,))
        conn.execute("INSERT INTO stock_lots(product_id,lot_number) VALUES(?, 'LOT-01')", (self.product_id,))
        lot_id = conn.execute("SELECT id FROM stock_lots WHERE product_id=?", (self.product_id,)).fetchone()["id"]
        conn.execute("""INSERT INTO stock_locations(warehouse_id,parent_id,code,name_ar,location_type)
            VALUES(?,NULL,'F1','الدور الأول','floor')""", (self.warehouse_id,))
        floor_id = conn.execute("SELECT id FROM stock_locations WHERE code='F1'").fetchone()["id"]
        conn.execute("""INSERT INTO stock_locations(warehouse_id,parent_id,code,name_ar,location_type)
            VALUES(?,?, 'Z1','منطقة التخزين','zone')""", (self.warehouse_id,floor_id))
        zone_id = conn.execute("SELECT id FROM stock_locations WHERE code='Z1'").fetchone()["id"]
        conn.execute("""INSERT INTO stock_locations(warehouse_id,parent_id,code,name_ar,location_type)
            VALUES(?,?, 'R1','الرف الأول','rack')""", (self.warehouse_id,zone_id))
        rack_id = conn.execute("SELECT id FROM stock_locations WHERE code='R1'").fetchone()["id"]
        for code in ("B1","B2"):
            conn.execute("""INSERT INTO stock_locations(warehouse_id,parent_id,code,name_ar,location_type)
                VALUES(?,?,?,?,'bin')""", (self.warehouse_id,rack_id,code,"خانة "+code))
        bin1 = conn.execute("SELECT id FROM stock_locations WHERE code='B1'").fetchone()["id"]
        movement_id = conn.execute("SELECT id FROM stock_movements WHERE movement_no='OPEN-1'").fetchone()["id"]
        conn.execute("""UPDATE stock_movements SET target_location_id=?,lot_id=? WHERE id=?""",
                     (bin1,lot_id,movement_id))
        conn.execute("""UPDATE stock_valuation_layers SET location_id=?,lot_id=? WHERE source_movement_id=?""",
                     (bin1,lot_id,movement_id))
        conn.commit()
        conn.close()

        response = self.client.post("/stock/counts", data={
            "warehouse_id": str(self.warehouse_id), "count_date": "2026-11-11",
        })
        self.assertEqual(response.status_code, 302)
        conn = stock.get_db()
        lines = conn.execute("""SELECT l.*,loc.code location_code,lot.lot_number
            FROM stocktake_lines l LEFT JOIN stock_locations loc ON loc.id=l.location_id
            LEFT JOIN stock_lots lot ON lot.id=l.lot_id ORDER BY loc.code""").fetchall()
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["location_code"], "B1")
        self.assertEqual(lines[0]["lot_number"], "LOT-01")
        self.assertEqual(lines[0]["snapshot_qty"], "10.0000")
        self.assertEqual(lines[1]["location_code"], "B2")
        self.assertEqual(lines[1]["snapshot_qty"], "0.0000")
        conn.close()

    def test_approval_is_blocked_if_stock_moved_after_count_started(self):
        self._login()
        self.client.post("/stock/counts", data={
            "warehouse_id": str(self.warehouse_id), "count_date": "2026-11-11",
        })
        conn = stock.get_db()
        header = conn.execute("SELECT * FROM stocktakes").fetchone()
        line = conn.execute("SELECT * FROM stocktake_lines").fetchone()
        self.client.post("/stock/counts/" + str(header["id"]), data={
            "counted_" + str(line["id"]): "10", "cost_" + str(line["id"]): "5",
        })
        conn.execute("""INSERT INTO stock_movements(movement_no,movement_type,product_id,
            target_warehouse_id,quantity,unit_cost,total_cost,movement_date)
            VALUES('AFTER-COUNT','receipt',?,?, '1','5','5','2026-11-11')""",
            (self.product_id,self.warehouse_id))
        conn.commit()
        response = self.client.post("/stock/counts/" + str(header["id"]) + "/approve")
        self.assertEqual(response.status_code, 302)
        status = conn.execute("SELECT status FROM stocktakes WHERE id=?", (header["id"],)).fetchone()["status"]
        self.assertEqual(status, "counted")
        conn.close()

    def test_stocktake_creation_is_blocked_in_closed_financial_year(self):
        self._login()
        response = self.client.post("/stock/counts", data={
            "warehouse_id": str(self.warehouse_id), "count_date": "2025-11-11",
        })
        self.assertEqual(response.status_code, 302)
        conn = stock.get_db()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM stocktakes").fetchone()[0], 0)
        conn.close()
        with self.client.session_transaction() as session:
            flashes = session.get("_flashes", [])
        self.assertTrue(any("السنة المالية 2025" in message for _category, message in flashes))

    def test_internal_transfer_is_blocked_in_locked_accounting_period(self):
        self._login()
        conn = stock.get_db()
        conn.execute("INSERT INTO stock_warehouses(code,name_ar) VALUES('W2','مخزن الوجهة')")
        target_id = conn.execute("SELECT id FROM stock_warehouses WHERE code='W2'").fetchone()["id"]
        conn.execute("INSERT INTO accounting_periods VALUES(1,'أكتوبر 2026','2026-10-01','2026-10-31',1)")
        conn.commit()
        conn.close()

        response = self.client.post("/stock/movements", data={
            "movement_type": "transfer",
            "product_id": str(self.product_id),
            "quantity": "1",
            "source_warehouse_id": str(self.warehouse_id),
            "target_warehouse_id": str(target_id),
            "movement_date": "2026-10-15",
        })
        self.assertEqual(response.status_code, 302)
        conn = stock.get_db()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM stock_movements").fetchone()[0], 1)
        conn.close()
        with self.client.session_transaction() as session:
            flashes = session.get("_flashes", [])
        self.assertTrue(any("الفترة المحاسبية مغلقة" in message for _category, message in flashes))

    def test_interbranch_transfer_full_flow_journal_stock_and_branch_analytics(self):
        self._login()
        conn = stock.get_db()
        source_branch = conn.execute(
            "INSERT INTO branches(code,name_ar) VALUES('BR-SRC','فرع المصدر')"
        ).lastrowid
        target_branch = conn.execute(
            "INSERT INTO branches(code,name_ar) VALUES('BR-DST','فرع الوجهة')"
        ).lastrowid
        conn.execute("UPDATE stock_warehouses SET branch_id=? WHERE id=?",
                     (source_branch,self.warehouse_id))
        conn.execute("INSERT INTO stock_warehouses(code,name_ar,branch_id) VALUES('W-DST','مخزن الوجهة',?)",
                     (target_branch,))
        target_warehouse = conn.execute(
            "SELECT id FROM stock_warehouses WHERE code='W-DST'"
        ).fetchone()["id"]
        conn.execute("INSERT INTO stock_warehouses(code,name_ar,branch_id) VALUES('W-SAME','مخزن نفس الفرع',?)",
                     (source_branch,))
        same_branch_warehouse = conn.execute(
            "SELECT id FROM stock_warehouses WHERE code='W-SAME'"
        ).fetchone()["id"]

        conn.execute("""INSERT INTO accounts(code,name_ar,account_type,is_group,active)
            VALUES('1185','تسوية تحويلات المخزون بين الفروع','asset',0,1)""")
        conn.executescript("""
            CREATE TABLE journal_entries(
                id INTEGER PRIMARY KEY,entry_date TEXT NOT NULL,reference TEXT NOT NULL UNIQUE,
                description TEXT NOT NULL,status TEXT NOT NULL,created_by INTEGER,created_at TEXT NOT NULL
            );
            CREATE TABLE journal_lines(
                id INTEGER PRIMARY KEY,entry_id INTEGER NOT NULL,account_id INTEGER NOT NULL,
                debit TEXT NOT NULL,credit TEXT NOT NULL,memo TEXT NOT NULL DEFAULT '',
                branch_id INTEGER
            );
        """)
        conn.commit()
        conn.close()

        def transfer(source, target, quantity, note):
            response = self.client.post("/stock/movements", data={
                "movement_type": "transfer", "product_id": str(self.product_id),
                "quantity": str(quantity), "source_warehouse_id": str(source),
                "target_warehouse_id": str(target), "movement_date": "2026-11-14",
                "notes": note,
            })
            self.assertEqual(response.status_code, 302)

        # Source has 10 units at 5 each. Transfer 4 units: value 20.
        transfer(self.warehouse_id,target_warehouse,4,"تحويل بين فرعين")
        conn = stock.get_db()
        first = conn.execute("""SELECT * FROM stock_movements
            WHERE movement_type='transfer' ORDER BY id DESC LIMIT 1""").fetchone()
        self.assertEqual(first["accounting_status"],"posted")
        self.assertIsNotNone(first["journal_entry_id"])
        self.assertEqual(Decimal(first["total_cost"]),Decimal("20.00"))

        source_qty = sum((Decimal(x["qty_remaining"]) for x in conn.execute(
            "SELECT qty_remaining FROM stock_valuation_layers WHERE warehouse_id=?",
            (self.warehouse_id,))),Decimal("0"))
        target_qty = sum((Decimal(x["qty_remaining"]) for x in conn.execute(
            "SELECT qty_remaining FROM stock_valuation_layers WHERE warehouse_id=?",
            (target_warehouse,))),Decimal("0"))
        self.assertEqual(source_qty,Decimal("6"))
        self.assertEqual(target_qty,Decimal("4"))

        first_lines = conn.execute("""SELECT jl.debit,jl.credit,jl.branch_id
            FROM journal_lines jl WHERE jl.entry_id=? ORDER BY jl.id""",
            (first["journal_entry_id"],)).fetchall()
        self.assertEqual(len(first_lines),2)
        branch_net = {source_branch:Decimal("0"),target_branch:Decimal("0")}
        for line in first_lines:
            branch_net[line["branch_id"]] += Decimal(line["debit"])-Decimal(line["credit"])
        self.assertEqual(branch_net[target_branch],Decimal("20"))
        self.assertEqual(branch_net[source_branch],Decimal("-20"))
        self.assertEqual(sum(branch_net.values()),Decimal("0"))

        # Reverse the same quantity: branch balances return to zero and stock returns.
        transfer(target_warehouse,self.warehouse_id,4,"عكس التحويل بين الفرعين")
        conn = stock.get_db()
        movements = conn.execute("""SELECT * FROM stock_movements
            WHERE movement_type='transfer' ORDER BY id""").fetchall()
        self.assertEqual(len(movements),2)
        self.assertTrue(all(m["accounting_status"]=="posted" for m in movements))
        self.assertEqual(len({m["journal_entry_id"] for m in movements}),2)
        source_qty = sum((Decimal(x["qty_remaining"]) for x in conn.execute(
            "SELECT qty_remaining FROM stock_valuation_layers WHERE warehouse_id=?",
            (self.warehouse_id,))),Decimal("0"))
        target_qty = sum((Decimal(x["qty_remaining"]) for x in conn.execute(
            "SELECT qty_remaining FROM stock_valuation_layers WHERE warehouse_id=?",
            (target_warehouse,))),Decimal("0"))
        self.assertEqual(source_qty,Decimal("10"))
        self.assertEqual(target_qty,Decimal("0"))
        total_stock_value = sum((Decimal(x["value_remaining"]) for x in conn.execute(
            "SELECT value_remaining FROM stock_valuation_layers")),Decimal("0"))
        self.assertEqual(total_stock_value,Decimal("50.00"))

        # Trial-balance control: account 1185 is balanced in total; analytical net by branch is zero.
        totals = conn.execute("""SELECT SUM(CAST(jl.debit AS NUMERIC)) debit,
            SUM(CAST(jl.credit AS NUMERIC)) credit
            FROM journal_lines jl JOIN accounts a ON a.id=jl.account_id
            WHERE a.code='1185'""").fetchone()
        self.assertEqual(Decimal(str(totals["debit"])),Decimal("40.00"))
        self.assertEqual(Decimal(str(totals["credit"])),Decimal("40.00"))
        branch_rows = conn.execute("""SELECT jl.branch_id,
            SUM(CAST(jl.debit AS NUMERIC))-SUM(CAST(jl.credit AS NUMERIC)) net
            FROM journal_lines jl JOIN accounts a ON a.id=jl.account_id
            WHERE a.code='1185' GROUP BY jl.branch_id""").fetchall()
        self.assertEqual({row["branch_id"]:Decimal(str(row["net"])) for row in branch_rows},
                         {source_branch:Decimal("0"),target_branch:Decimal("0")})

        # A transfer between warehouses of the same branch moves stock without a GL journal.
        transfer(self.warehouse_id,same_branch_warehouse,1,"تحويل داخلي داخل نفس الفرع")
        conn = stock.get_db()
        same = conn.execute("""SELECT * FROM stock_movements
            WHERE movement_type='transfer' ORDER BY id DESC LIMIT 1""").fetchone()
        self.assertEqual(same["accounting_status"],"not_required")
        self.assertIsNone(same["journal_entry_id"])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0],2)
        conn.close()

    def test_stocktake_approval_is_blocked_if_year_closed_after_count(self):
        self._login()
        self.client.post("/stock/counts", data={
            "warehouse_id": str(self.warehouse_id), "count_date": "2026-11-11",
        })
        conn = stock.get_db()
        header = conn.execute("SELECT * FROM stocktakes").fetchone()
        line = conn.execute("SELECT * FROM stocktake_lines").fetchone()
        self.client.post("/stock/counts/" + str(header["id"]), data={
            "counted_" + str(line["id"]): "10", "cost_" + str(line["id"]): "5",
        })
        conn.execute("UPDATE financial_years SET status='closed' WHERE id=2")
        conn.commit()
        response = self.client.post("/stock/counts/" + str(header["id"]) + "/approve")
        self.assertEqual(response.status_code, 302)
        status = conn.execute("SELECT status FROM stocktakes WHERE id=?", (header["id"],)).fetchone()["status"]
        self.assertEqual(status, "counted")
        conn.close()

    def test_stocktake_screen_requires_stock_management_permission(self):
        with self.client.session_transaction() as session:
            session.update(user_id=7, username="viewer", role="viewer", permissions=["stock.view"])
        response = self.client.get("/stock/counts")
        self.assertEqual(response.status_code, 302)


    def test_stocktake_shortage_and_surplus_both_use_6990(self):
        self._login()
        conn = stock.get_db()
        second_product = conn.execute(
            "INSERT INTO stock_products(sku,name_ar) VALUES('P2','صنف زيادة')"
        ).lastrowid
        second_receipt = conn.execute("""INSERT INTO stock_movements(
            movement_no,movement_type,product_id,target_warehouse_id,quantity,unit_cost,
            total_cost,movement_date)
            VALUES('OPEN-2','receipt',?,?,'10','5','50','2026-01-01')""",
            (second_product,self.warehouse_id)).lastrowid
        conn.execute("""INSERT INTO stock_valuation_layers(
            product_id,warehouse_id,source_movement_id,qty_in,qty_remaining,
            unit_cost,value_remaining)
            VALUES(?,?,?,'10','10','5','50')""",
            (second_product,self.warehouse_id,second_receipt))
        conn.execute("""CREATE TABLE company_settings(
            setting_key TEXT PRIMARY KEY,setting_value TEXT)""")
        conn.execute("INSERT INTO company_settings VALUES('activity','industrial')")
        conn.execute("INSERT INTO company_settings VALUES('inventory_cost_method','fifo')")
        conn.commit()
        conn.close()

        self.client.post("/stock/counts", data={
            "warehouse_id": str(self.warehouse_id), "count_date": "2026-11-11",
        })
        conn = stock.get_db()
        header = conn.execute("SELECT * FROM stocktakes").fetchone()
        lines = conn.execute("SELECT * FROM stocktake_lines ORDER BY product_id").fetchall()
        self.assertEqual(len(lines), 2)
        counts = {lines[0]["id"]: "8", lines[1]["id"]: "12"}
        response = self.client.post("/stock/counts/" + str(header["id"]), data={
            **{"counted_" + str(line_id): qty for line_id, qty in counts.items()},
            **{"cost_" + str(line["id"]): "7" for line in lines},
        })
        self.assertEqual(response.status_code, 302)

        with patch("stock.post_journal", return_value=777) as post:
            response = self.client.post("/stock/counts/" + str(header["id"]) + "/approve")
        self.assertEqual(response.status_code, 302)
        post.assert_called_once()
        journal_lines = post.call_args.args[4]
        variance_id = conn.execute("SELECT id FROM accounts WHERE code='6990'").fetchone()["id"]
        inventory_id = conn.execute("SELECT id FROM accounts WHERE code='1130'").fetchone()["id"]
        self.assertEqual(
            [(line[0], Decimal(str(line[1])), Decimal(str(line[2]))) for line in journal_lines],
            [
                (variance_id, Decimal("10.00"), Decimal("0")),
                (inventory_id, Decimal("0"), Decimal("10.00")),
                (inventory_id, Decimal("14.00"), Decimal("0")),
                (variance_id, Decimal("0"), Decimal("14.00")),
            ],
        )
        movements = conn.execute("""SELECT movement_type,accounting_status,journal_entry_id,total_cost
            FROM stock_movements WHERE movement_no LIKE 'STK-%' OR notes IN ('عجز جرد فعلي','زيادة جرد فعلي')
            ORDER BY id DESC LIMIT 2""").fetchall()
        self.assertEqual(len(movements), 2)
        self.assertTrue(all(row["accounting_status"] == "posted" for row in movements))
        self.assertTrue(all(row["journal_entry_id"] == 777 for row in movements))
        header = conn.execute("SELECT status,journal_entry_id FROM stocktakes WHERE id=?",
                              (header["id"],)).fetchone()
        self.assertEqual(header["status"], "approved")
        self.assertEqual(header["journal_entry_id"], 777)
        conn.close()


    def test_service_goods_requires_explicit_valid_inventory_account(self):
        self._login()
        conn = stock.get_db()
        initialize_activity_profiles_schema(conn)
        conn.execute("""CREATE TABLE company_settings(
            setting_key TEXT PRIMARY KEY,setting_value TEXT)""")
        conn.execute("INSERT INTO company_settings VALUES('activity','service')")
        conn.commit()
        conn.close()

        unit = stock.get_db().execute(
            "SELECT id FROM stock_uoms WHERE code='PCS'"
        ).fetchone()["id"]
        form = {
            "sku": "SERVICE-GOODS-1", "name_ar": "مخزون خدمي",
            "base_uom_id": str(unit), "product_type": "goods",
            "purchase_price": "10", "sale_price": "15",
        }
        response = self.client.post("/stock/products", data=form)
        self.assertEqual(response.status_code, 302)
        conn = stock.get_db()
        self.assertIsNone(conn.execute(
            "SELECT id FROM stock_products WHERE sku LIKE 'ITM-%' ORDER BY id DESC LIMIT 1"
        ).fetchone())
        conn.close()

        conn = stock.get_db()
        profile_id = create_activity_profile(
            conn, "service_with_inventory_test", "خدمي بمخزون اختباري", "service"
        )
        profile = conn.execute(
            "SELECT posting_accounts_json FROM activity_profiles WHERE id=?",
            (profile_id,)
        ).fetchone()
        posting = json.loads(profile["posting_accounts_json"])
        posting["inventory"] = "1130"
        conn.execute(
            "UPDATE activity_profiles SET posting_accounts_json=? WHERE id=?",
            (json.dumps(posting),profile_id)
        )
        conn.execute(
            "INSERT INTO company_settings(setting_key,setting_value) VALUES('activity_profile_code',?)",
            ("service_with_inventory_test",)
        )
        conn.commit()
        conn.close()

        response = self.client.post("/stock/products", data=form)
        self.assertEqual(response.status_code, 302)
        conn = stock.get_db()
        created = conn.execute(
            "SELECT product_type FROM stock_products WHERE sku='SERVICE-GOODS-1'"
        ).fetchone()
        self.assertIsNotNone(created)
        self.assertEqual(created["product_type"], "goods")
        conn.close()


    def test_valuation_reconciliation_compares_remaining_layer_to_posted_gl(self):
        self._login()
        with self.client.session_transaction() as session:
            session["permissions"] = ["stock.manage", "stock.view"]
        conn = stock.get_db()
        conn.execute("INSERT INTO accounts(code,name_ar,account_type,is_group,active) VALUES('1150','مخزون بضاعة','asset',0,1)")
        initialize_activity_profiles_schema(conn)
        conn.execute("""CREATE TABLE company_settings(
            setting_key TEXT PRIMARY KEY,setting_value TEXT)""")
        conn.execute("INSERT INTO company_settings VALUES('activity','commercial')")
        conn.execute("""CREATE TABLE journal_entries(
            id INTEGER PRIMARY KEY,entry_date TEXT,reference TEXT,description TEXT,status TEXT)""")
        conn.execute("""CREATE TABLE journal_lines(
            id INTEGER PRIMARY KEY,entry_id INTEGER,account_id INTEGER,debit TEXT,credit TEXT,memo TEXT)""")
        inventory_id = conn.execute("SELECT id FROM accounts WHERE code='1150'").fetchone()["id"]
        conn.execute("INSERT INTO journal_entries(id,entry_date,reference,description,status) VALUES(1,'2026-01-01','OPEN','رصيد افتتاحي','posted')")
        conn.execute("INSERT INTO journal_lines(entry_id,account_id,debit,credit,memo) VALUES(1,?,'50','0','مخزون افتتاحي')",
                     (inventory_id,))
        conn.commit()
        conn.close()

        response = self.client.get("/stock/reconciliation")
        self.assertEqual(response.status_code, 200)
        self.assertIn("1150".encode(), response.data)
        self.assertIn("50".encode(), response.data)
        self.assertNotIn("دفتر الأستاذ غير مهيأ".encode(), response.data)


    def test_pending_purpose_and_setup_can_be_resolved_from_stock_queue(self):
        self._login()
        with self.client.session_transaction() as session:
            session["permissions"] = ["stock.manage", "stock.view"]
        conn = stock.get_db()
        pending_purpose_id = conn.execute("""INSERT INTO stock_movements(
            movement_no,movement_type,product_id,source_warehouse_id,quantity,unit_cost,
            total_cost,movement_date,accounting_status)
            VALUES('PENDING-PURPOSE','issue',?,?,'2','5','10','2026-11-14','pending_purpose')""",
            (self.product_id, self.warehouse_id)).lastrowid
        pending_setup_id = conn.execute("""INSERT INTO stock_movements(
            movement_no,movement_type,product_id,source_warehouse_id,quantity,unit_cost,
            total_cost,movement_date,accounting_status,issue_purpose)
            VALUES('PENDING-SETUP','issue',?,?,'2','5','10','2026-11-14','pending_setup','sale')""",
            (self.product_id, self.warehouse_id)).lastrowid
        conn.commit()
        conn.close()

        queue = self.client.get("/stock/pending")
        self.assertEqual(queue.status_code, 200)
        self.assertIn(b"PENDING-PURPOSE", queue.data)
        self.assertIn(b"PENDING-SETUP", queue.data)
        self.assertIn(b'href="/stock/reconciliation"', queue.data)
        self.assertIn(b'href="/stock/pending"', queue.data)

        def fake_post_issue(conn, movement_id, movement_no, product, movement_date,
                            total_cost, created_by, purpose, dimensions=None):
            self.assertIn(purpose, {"sale", "internal"})
            conn.execute(
                "UPDATE stock_movements SET journal_entry_id=777,accounting_status='posted' WHERE id=?",
                (movement_id,),
            )
            return 777

        with patch("stock.post_issue_journal", side_effect=fake_post_issue) as post:
            first = self.client.post(
                f"/stock/pending/{pending_purpose_id}/resolve",
                data={"issue_purpose": "sale"},
            )
            second = self.client.post(
                f"/stock/pending/{pending_setup_id}/resolve", data={},
            )
        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 302)
        self.assertEqual(post.call_count, 2)

        conn = stock.get_db()
        rows = conn.execute(
            "SELECT issue_purpose,accounting_status,journal_entry_id FROM stock_movements WHERE id IN (?,?) ORDER BY id",
            (pending_purpose_id, pending_setup_id),
        ).fetchall()
        self.assertEqual(rows[0]["issue_purpose"], "sale")
        self.assertTrue(all(row["accounting_status"] == "posted" for row in rows))
        self.assertTrue(all(row["journal_entry_id"] == 777 for row in rows))
        conn.close()


    def test_pending_document_cannot_be_reposted_from_stock_queue(self):
        self._login()
        conn = stock.get_db()
        movement_id = conn.execute("""INSERT INTO stock_movements(
            movement_no,movement_type,product_id,quantity,unit_cost,total_cost,
            movement_date,accounting_status)
            VALUES('PENDING-DOC','receipt',?,'2','5','10','2026-11-14','pending_document')""",
            (self.product_id,)).lastrowid
        conn.commit()
        conn.close()

        response = self.client.post(
            "/stock/pending/" + str(movement_id) + "/resolve", data={}
        )
        self.assertEqual(response.status_code, 302)
        conn = stock.get_db()
        saved = conn.execute(
            "SELECT accounting_status,journal_entry_id FROM stock_movements WHERE id=?",
            (movement_id,)
        ).fetchone()
        self.assertEqual(saved["accounting_status"], "pending_document")
        self.assertIsNone(saved["journal_entry_id"])
        conn.close()


if __name__ == "__main__":
    unittest.main()
