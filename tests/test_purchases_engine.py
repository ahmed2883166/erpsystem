import sqlite3
from decimal import Decimal
import tempfile
import unittest
from pathlib import Path
from flask import Flask
from unittest.mock import patch

import purchases
import stock
import fixed_assets


class PurchasingAccountingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db_path = Path(self.tmp.name) / "nexus.db"
        purchases.DB_PATH = db_path
        stock.DB_PATH = db_path
        fixed_assets.DB_PATH = db_path
        stock.initialize_stock_schema()
        self.db = purchases.db()
        self.db.executescript("""
        CREATE TABLE accounts(
          id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL, name_ar TEXT NOT NULL,
          account_type TEXT NOT NULL, parent_id INTEGER, is_group INTEGER NOT NULL DEFAULT 0,
          active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE journal_entries(
          id INTEGER PRIMARY KEY, entry_date TEXT NOT NULL, reference TEXT, description TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'posted', created_by INTEGER, created_at TEXT NOT NULL
        );
        CREATE TABLE journal_lines(
          id INTEGER PRIMARY KEY, entry_id INTEGER NOT NULL, account_id INTEGER NOT NULL,
          debit TEXT NOT NULL DEFAULT '0', credit TEXT NOT NULL DEFAULT '0', memo TEXT
        );
        CREATE TABLE company_settings(setting_key TEXT PRIMARY KEY, setting_value TEXT NOT NULL);
        """)
        self.db.execute("INSERT INTO accounts(code,name_ar,account_type,is_group) VALUES('2000','الالتزامات','liability',1)")
        self.db.execute("INSERT INTO accounts(code,name_ar,account_type,is_group) VALUES('2190','GRNI','liability',0)")
        self.db.execute("INSERT INTO accounts(code,name_ar,account_type,is_group) VALUES('1100','الأصول المتداولة','asset',1)")
        self.db.execute("INSERT INTO accounts(code,name_ar,account_type,is_group) VALUES('1110','البنك','asset',0)")
        self.db.execute("INSERT INTO accounts(code,name_ar,account_type,is_group) VALUES('5000','مصروف','expense',0)")
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_purchase_schema_is_created(self):
        purchases.init_purchase_schema()
        names = {r["name"] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("suppliers", names)
        self.assertIn("supplier_bills", names)
        self.assertIn("supplier_payments", names)
        self.assertIn("supplier_payment_allocations", names)
        self.assertIn("purchase_requests", names)

    def test_supplier_invoice_is_unique_and_receipt_can_be_allocated_partially(self):
        purchases.init_purchase_schema()
        self.db.execute("INSERT INTO suppliers(code,name) VALUES('SUP-1','مورد اختبار')")
        supplier_id = self.db.execute("SELECT id FROM suppliers WHERE code='SUP-1'").fetchone()["id"]
        self.db.execute("INSERT INTO stock_warehouses(code,name_ar) VALUES('W1','مخزن')")
        warehouse_id = self.db.execute("SELECT id FROM stock_warehouses WHERE code='W1'").fetchone()["id"]
        self.db.execute("""INSERT INTO goods_receipts(receipt_no,supplier_id,warehouse_id,receipt_date,total_value)
                           VALUES('GR-1',?,?, '2026-11-10','100')""", (supplier_id, warehouse_id))
        receipt_id = self.db.execute("SELECT id FROM goods_receipts WHERE receipt_no='GR-1'").fetchone()["id"]
        for bill_no, invoice_no in (("BILL-1", "DUP"), ("BILL-2", "UNIQUE-2")):
            self.db.execute("""INSERT INTO supplier_bills(bill_no,supplier_id,bill_date,supplier_invoice_no,
                               subtotal,tax_rate,tax_amount,total_amount)
                               VALUES(?,?,'2026-11-10',?,'40','0','0','40')""",
                            (bill_no, supplier_id, invoice_no))
        bill1 = self.db.execute("SELECT id FROM supplier_bills WHERE bill_no='BILL-1'").fetchone()["id"]
        bill2 = self.db.execute("SELECT id FROM supplier_bills WHERE bill_no='BILL-2'").fetchone()["id"]
        self.db.execute("INSERT INTO supplier_bill_receipts(bill_id,receipt_id,matched_amount) VALUES(?,?,?)",
                        (bill1, receipt_id, "40"))
        self.db.execute("INSERT INTO supplier_bill_receipts(bill_id,receipt_id,matched_amount) VALUES(?,?,?)",
                        (bill2, receipt_id, "60"))
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("""INSERT INTO supplier_bills(bill_no,supplier_id,bill_date,supplier_invoice_no,
                               subtotal,tax_rate,tax_amount,total_amount)
                               VALUES('BILL-3',?,'2026-11-10',' dup ','100','0','0','100')""", (supplier_id,))
        self.db.commit()
        available = purchases._supplier_receipt_available_amount(self.db, receipt_id)
        self.assertEqual(available, purchases.money("0"))
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM supplier_bill_receipts WHERE receipt_id=?", (receipt_id,)
        ).fetchone()[0], 2)

    def test_two_partial_goods_receipts_create_fifo_layers_and_balanced_grni_entries(self):
        purchases.init_purchase_schema()
        self.db.execute(
            "INSERT INTO accounts(code,name_ar,account_type,is_group) VALUES('1150','مخزون','asset',0)"
        )
        self.db.execute("INSERT INTO company_settings(setting_key,setting_value) VALUES('activity','commercial')")
        supplier_id = self.db.execute(
            "INSERT INTO suppliers(code,name) VALUES('SUP-FIFO-CYCLE','مورد دورة مخزون')"
        ).lastrowid
        warehouse_id = self.db.execute(
            "INSERT INTO stock_warehouses(code,name_ar) VALUES('W-FIFO-CYCLE','مخزن دورة مخزون')"
        ).lastrowid
        product_id = self.db.execute(
            "INSERT INTO stock_products(sku,name_ar) VALUES('FIFO-CYCLE-1','صنف دورة مخزون')"
        ).lastrowid
        po_id = self.db.execute(
            """INSERT INTO purchase_orders(po_no,supplier_id,order_date,status,created_by)
               VALUES('PO-FIFO-CYCLE',?,'2026-11-15','approved',1)""",
            (supplier_id,),
        ).lastrowid
        self.db.execute(
            """INSERT INTO purchase_order_lines(po_id,product_id,quantity,unit_price)
               VALUES(?,?, '10','11')""",
            (po_id, product_id),
        )

        app = Flask(__name__)
        app.secret_key = "test"
        app.add_url_rule("/__test_global_search", endpoint="global_search", view_func=lambda: "")
        app.register_blueprint(purchases.purchase_bp)
        client = app.test_client()
        with client.session_transaction() as session:
            session["user_id"] = 1
            session["role"] = "admin"

        for quantity, unit_cost, expected_status in (
            ("4", "10", "partially_received"),
            ("6", "12", "received"),
        ):
            response = client.post("/purchases/receipts/new", data={
                "supplier_id": str(supplier_id),
                "product_id": str(product_id),
                "warehouse_id": str(warehouse_id),
                "po_id": str(po_id),
                "receipt_date": "2026-11-15",
                "quantity": quantity,
                "unit_cost": unit_cost,
            })
            self.assertEqual(response.status_code, 302)
            self.assertEqual(
                self.db.execute("SELECT status FROM purchase_orders WHERE id=?", (po_id,)).fetchone()[0],
                expected_status,
            )

        receipts = self.db.execute(
            "SELECT total_value,journal_entry_id FROM goods_receipts WHERE po_id=? ORDER BY id",
            (po_id,),
        ).fetchall()
        self.assertEqual([Decimal(row["total_value"]) for row in receipts],
                         [Decimal("40.00"), Decimal("72.00")])
        self.assertTrue(all(row["journal_entry_id"] for row in receipts))
        for receipt in receipts:
            rows = self.db.execute(
                """SELECT a.code,jl.debit,jl.credit FROM journal_lines jl
                   JOIN accounts a ON a.id=jl.account_id WHERE jl.entry_id=?""",
                (receipt["journal_entry_id"],),
            ).fetchall()
            debit = sum((Decimal(row["debit"]) for row in rows), Decimal("0"))
            credit = sum((Decimal(row["credit"]) for row in rows), Decimal("0"))
            self.assertEqual(debit, credit)
            self.assertEqual(debit, Decimal(receipt["total_value"]))

        movement_rows = self.db.execute(
            """SELECT movement_type,accounting_status,journal_entry_id,quantity,total_cost
               FROM stock_movements WHERE product_id=? ORDER BY id""",
            (product_id,),
        ).fetchall()
        self.assertEqual(len(movement_rows), 2)
        self.assertTrue(all(row["movement_type"] == "receipt" and
                            row["accounting_status"] == "posted" and row["journal_entry_id"]
                            for row in movement_rows))
        layers = self.db.execute(
            """SELECT qty_in,qty_remaining,unit_cost FROM stock_valuation_layers
               WHERE product_id=? ORDER BY id""",
            (product_id,),
        ).fetchall()
        self.assertEqual(
            [(Decimal(row["qty_in"]), Decimal(row["qty_remaining"]), Decimal(row["unit_cost"]))
             for row in layers],
            [(Decimal("4"), Decimal("4"), Decimal("10")),
             (Decimal("6"), Decimal("6"), Decimal("12"))],
        )

        fifo_cost, allocations = stock.consume_layers(
            self.db, product_id, warehouse_id, Decimal("5"), "fifo"
        )
        self.assertEqual(fifo_cost, Decimal("52.00"))
        self.assertEqual(
            [(allocation[1], allocation[2]) for allocation in allocations],
            [(Decimal("4"), Decimal("10")), (Decimal("1"), Decimal("12"))],
        )
        self.db.execute(
            """INSERT INTO stock_movements(
                movement_no,movement_type,product_id,source_warehouse_id,quantity,
                unit_cost,total_cost,movement_date,accounting_status)
               VALUES('FIFO-CYCLE-ISSUE','issue',?,?, '5','10.4','52','2026-11-15','posted')""",
            (product_id, warehouse_id),
        )
        remaining = self.db.execute(
            """SELECT qty_remaining,value_remaining FROM stock_valuation_layers
               WHERE product_id=? ORDER BY id""",
            (product_id,),
        ).fetchall()
        self.assertEqual([Decimal(row["qty_remaining"]) for row in remaining],
                         [Decimal("0"), Decimal("5")])
        self.assertEqual([Decimal(row["value_remaining"]) for row in remaining],
                         [Decimal("0.00"), Decimal("60.00")])
        self.assertEqual(
            Decimal(stock.current_qty(self.db, product_id, warehouse_id)),
            Decimal("5"),
        )

        # Post the real COGS journal for the FIFO issue, then clear GRNI with
        # a supplier invoice and record a partial payment against that invoice.
        self.db.execute(
            "INSERT INTO accounts(code,name_ar,account_type,is_group) VALUES('2110','الموردون','liability',0)"
        )
        self.db.execute(
            "INSERT INTO accounts(code,name_ar,account_type,is_group) VALUES('5100','تكلفة المبيعات','expense',0)"
        )
        movement_id = self.db.execute(
            "SELECT id FROM stock_movements WHERE movement_no='FIFO-CYCLE-ISSUE'"
        ).fetchone()["id"]
        with patch("activity_profiles.active_posting_accounts", return_value={
            "inventory": "1150", "cogs": "5100", "supplier_ap": "2110",
            "purchase_inventory": "1150",
        }), patch("activity_profiles.resolve_product_account_code",
                 side_effect=lambda _c, _p, role, default: "5100" if role == "cogs" else "1150"):
            stock.post_issue_journal(
                self.db, movement_id, "FIFO-CYCLE-ISSUE",
                {"id": product_id, "name_ar": "صنف دورة مخزون"},
                "2026-11-15", Decimal("52"), 1, "sale",
            )

        receipt_ids = [row["id"] for row in self.db.execute(
            "SELECT id FROM goods_receipts WHERE po_id=? ORDER BY id", (po_id,)
        )]
        bill_id = self.db.execute(
            """INSERT INTO supplier_bills(
                bill_no,supplier_id,bill_date,due_date,supplier_invoice_no,
                subtotal,tax_rate,tax_amount,total_amount,created_by,status,bill_kind
            ) VALUES('BILL-FIFO-CYCLE',?,'2026-11-15','2026-12-15','INV-FIFO-CYCLE',
                     '112','0','0','112',1,'approved','inventory')""",
            (supplier_id,),
        ).lastrowid
        for receipt_id, amount in zip(receipt_ids, ("40", "72")):
            self.db.execute(
                "INSERT INTO supplier_bill_receipts(bill_id,receipt_id,matched_amount) VALUES(?,?,?)",
                (bill_id, receipt_id, amount),
            )
        self.db.commit()
        with patch("purchases._activity_posting_accounts", return_value={
            "inventory": "1150", "cogs": "5100", "supplier_ap": "2110",
            "purchase_inventory": "1150",
        }):
            bill_entry_id = purchases.post_supplier_bill(self.db, bill_id, 1)
        self.db.commit()

        # Supplier invoice: debit GRNI 112 / credit AP 112.
        bill_lines = self.db.execute(
            """SELECT a.code,jl.debit,jl.credit FROM journal_lines jl
               JOIN accounts a ON a.id=jl.account_id WHERE jl.entry_id=?""",
            (bill_entry_id,),
        ).fetchall()
        bill_totals = {}
        for row in bill_lines:
            debit, credit = bill_totals.get(row["code"], (Decimal("0"), Decimal("0")))
            bill_totals[row["code"]] = (
                debit + Decimal(row["debit"]),
                credit + Decimal(row["credit"]),
            )
        self.assertEqual(bill_totals["2190"], (Decimal("112.00"), Decimal("0.00")))
        self.assertEqual(bill_totals["2110"], (Decimal("0.00"), Decimal("112.00")))

        app2 = Flask(__name__)
        app2.secret_key = "test"
        app2.add_url_rule("/__test_global_search", endpoint="global_search", view_func=lambda: "")
        app2.register_blueprint(purchases.purchase_bp)
        payment_client = app2.test_client()
        with payment_client.session_transaction() as session:
            session["user_id"] = 1
            session["role"] = "admin"
        with patch("purchases._activity_posting_accounts", return_value={
            "inventory": "1150", "cogs": "5100", "supplier_ap": "2110",
            "purchase_inventory": "1150",
        }):
            payment_response = payment_client.post("/purchases/payments/new", data={
                "supplier_id": str(supplier_id),
                "amount": "50",
                "cash_account_id": str(self.db.execute(
                    "SELECT id FROM accounts WHERE code='1110'"
                ).fetchone()["id"]),
                "payment_date": "2026-11-15",
                "reference": "PARTIAL-FIFO-CYCLE",
            })
        self.assertEqual(payment_response.status_code, 302)
        bill = self.db.execute(
            "SELECT paid_amount,status FROM supplier_bills WHERE id=?", (bill_id,)
        ).fetchone()
        self.assertEqual(bill["status"], "posted")
        self.assertEqual(Decimal(bill["paid_amount"]), Decimal("50.00"))
        allocation = self.db.execute(
            "SELECT amount FROM supplier_payment_allocations WHERE bill_id=?", (bill_id,)
        ).fetchone()
        self.assertEqual(Decimal(allocation["amount"]), Decimal("50.00"))

        # Inventory subledger 5 units / 60.00 must equal posted GL inventory 60.00.
        inventory_gl = self.db.execute(
            """SELECT COALESCE(SUM(CAST(jl.debit AS NUMERIC)-CAST(jl.credit AS NUMERIC)),0)
               FROM journal_lines jl JOIN journal_entries je ON je.id=jl.entry_id
               JOIN accounts a ON a.id=jl.account_id
               WHERE je.status='posted' AND a.code='1150'"""
        ).fetchone()[0]
        layer_value = sum((Decimal(row["value_remaining"]) for row in self.db.execute(
            "SELECT value_remaining FROM stock_valuation_layers WHERE product_id=?",
            (product_id,),
        )), Decimal("0"))
        self.assertEqual(Decimal(str(inventory_gl)), Decimal("60"))
        self.assertEqual(layer_value, Decimal("60.00"))
        self.assertEqual(Decimal(str(inventory_gl)), layer_value)

        # Exercise the actual inventory-vs-GL reconciliation screen against
        # the same isolated database and require a matched result.
        report_app = Flask(__name__)
        report_app.secret_key = "test"
        report_app.add_url_rule("/__test_global_search", endpoint="global_search", view_func=lambda: "")
        report_app.register_blueprint(stock.stock_bp)
        report_client = report_app.test_client()
        with report_client.session_transaction() as session:
            session["user_id"] = 1
            session["role"] = "admin"
        with (
            patch("activity_profiles.active_posting_accounts", return_value={"inventory": "1150"}),
            patch("activity_profiles.get_active_activity_profile", return_value={
                "account_roles": {"inventory": ["1150"]}
            }),
            patch("activity_profiles.resolve_product_account_code", return_value="1150"),
            patch("stock.render_template", side_effect=lambda _template, **context:
                  "|".join(str(context.get(key)) for key in (
                      "reconciliation_status", "total_valuation", "total_ledger", "total_difference", "error"
                  ))),
        ):
            report_response = report_client.get("/stock/reconciliation")
        self.assertEqual(report_response.status_code, 200)
        report_values = report_response.get_data(as_text=True)
        self.assertIn("matched", report_values)
        self.assertIn("60", report_values)

        # Every journal entry created by the full cycle must balance.
        unbalanced = self.db.execute(
            """SELECT je.id,SUM(CAST(jl.debit AS NUMERIC)) debit,
                      SUM(CAST(jl.credit AS NUMERIC)) credit
               FROM journal_entries je JOIN journal_lines jl ON jl.entry_id=je.id
               GROUP BY je.id
               HAVING ABS(SUM(CAST(jl.debit AS NUMERIC))-SUM(CAST(jl.credit AS NUMERIC)))>0.005"""
        ).fetchall()
        self.assertEqual(unbalanced, [])

    def test_multi_line_purchase_order_stays_partial_until_every_line_is_received(self):
        purchases.init_purchase_schema()
        self.db.execute("INSERT INTO suppliers(code,name) VALUES('SUP-MULTI-RECV','مورد متعدد')")
        supplier_id = self.db.execute(
            "SELECT id FROM suppliers WHERE code='SUP-MULTI-RECV'"
        ).fetchone()["id"]
        self.db.execute("INSERT INTO stock_products(id,sku,name_ar) VALUES(901,'PO-MULTI-A','صنف أ')")
        self.db.execute("INSERT INTO stock_products(id,sku,name_ar) VALUES(902,'PO-MULTI-B','صنف ب')")
        self.db.execute("""INSERT INTO purchase_orders(po_no,supplier_id,order_date,created_by,status)
                           VALUES('PO-MULTI-RECV',?,'2026-11-15',1,'approved')""", (supplier_id,))
        po_id = self.db.execute(
            "SELECT id FROM purchase_orders WHERE po_no='PO-MULTI-RECV'"
        ).fetchone()["id"]
        self.db.executemany(
            "INSERT INTO purchase_order_lines(po_id,product_id,quantity,received_qty,unit_price) VALUES(?,?,?,?,?)",
            [(po_id, 901, "5", "5", "10"), (po_id, 902, "8", "0", "20")],
        )
        self.db.commit()

        status = purchases.refresh_purchase_order_receipt_status(self.db, po_id)
        self.assertEqual(status, "partially_received")
        self.assertEqual(
            self.db.execute("SELECT status FROM purchase_orders WHERE id=?", (po_id,)).fetchone()[0],
            "partially_received",
        )

        self.db.execute(
            "UPDATE purchase_order_lines SET received_qty='8' WHERE po_id=? AND product_id=902",
            (po_id,),
        )
        status = purchases.refresh_purchase_order_receipt_status(self.db, po_id)
        self.assertEqual(status, "received")
        self.assertEqual(
            self.db.execute("SELECT status FROM purchase_orders WHERE id=?", (po_id,)).fetchone()[0],
            "received",
        )

    def test_purchase_order_requires_independent_approval_before_receipt(self):
        purchases.init_purchase_schema()
        self.db.execute("INSERT INTO suppliers(code,name) VALUES('SUP-APP','مورد اعتماد')")
        supplier_id = self.db.execute("SELECT id FROM suppliers WHERE code='SUP-APP'").fetchone()["id"]
        self.db.execute("INSERT INTO stock_products(id,sku,name_ar) VALUES(1,'PO-ITEM-1','صنف اختبار')")
        self.db.execute("""INSERT INTO purchase_orders(po_no,supplier_id,order_date,created_by)
                           VALUES('PO-APP-1',?,'2026-11-13',1)""", (supplier_id,))
        order_id = self.db.execute("SELECT id FROM purchase_orders WHERE po_no='PO-APP-1'").fetchone()["id"]
        self.db.execute("INSERT INTO purchase_order_lines(po_id,product_id,quantity,unit_price) VALUES(?,?,?,?)",
                        (order_id, 1, "3", "25"))
        self.db.commit()

        purchases.submit_purchase_order(self.db, order_id, 1)
        self.db.commit()
        state = self.db.execute("SELECT status,submitted_by FROM purchase_orders WHERE id=?", (order_id,)).fetchone()
        self.assertEqual(state["status"], "pending_approval")
        self.assertEqual(state["submitted_by"], 1)

        with self.assertRaisesRegex(ValueError, "لا يجوز اعتماد"):
            purchases.approve_purchase_order(self.db, order_id, 1)
        self.db.rollback()
        self.assertEqual(self.db.execute("SELECT status FROM purchase_orders WHERE id=?", (order_id,)).fetchone()[0],
                         "pending_approval")

        purchases.approve_purchase_order(self.db, order_id, 2, "مطابقة السعر والكمية")
        self.db.commit()
        approved = self.db.execute(
            "SELECT status,approved_by,approved_at,approval_reason FROM purchase_orders WHERE id=?", (order_id,)
        ).fetchone()
        self.assertEqual(approved["status"], "approved")
        self.assertEqual(approved["approved_by"], 2)
        self.assertTrue(approved["approved_at"])
        self.assertEqual(approved["approval_reason"], "مطابقة السعر والكمية")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM stock_movements").fetchone()[0], 0)

        with self.assertRaisesRegex(ValueError, "ليس بانتظار الاعتماد"):
            purchases.approve_purchase_order(self.db, order_id, 3)
        self.db.rollback()

    def test_purchase_order_rejection_requires_reason_and_can_be_resubmitted(self):
        purchases.init_purchase_schema()
        self.db.execute("INSERT INTO suppliers(code,name) VALUES('SUP-REJ','مورد رفض')")
        supplier_id = self.db.execute("SELECT id FROM suppliers WHERE code='SUP-REJ'").fetchone()["id"]
        self.db.execute("INSERT INTO stock_products(id,sku,name_ar) VALUES(1,'PO-ITEM-2','صنف اختبار')")
        self.db.execute("""INSERT INTO purchase_orders(po_no,supplier_id,order_date,created_by)
                           VALUES('PO-REJ-1',?,'2026-11-13',1)""", (supplier_id,))
        order_id = self.db.execute("SELECT id FROM purchase_orders WHERE po_no='PO-REJ-1'").fetchone()["id"]
        self.db.execute("INSERT INTO purchase_order_lines(po_id,product_id,quantity,unit_price) VALUES(?,?,?,?)",
                        (order_id, 1, "2", "40"))
        self.db.commit()
        purchases.submit_purchase_order(self.db, order_id, 1)
        self.db.commit()
        with self.assertRaisesRegex(ValueError, "سبب رفض"):
            purchases.reject_purchase_order(self.db, order_id, 2, "لا")
        self.db.rollback()
        purchases.reject_purchase_order(self.db, order_id, 2, "السعر أعلى من العرض")
        self.db.commit()
        rejected = self.db.execute("SELECT status,rejection_reason FROM purchase_orders WHERE id=?", (order_id,)).fetchone()
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(rejected["rejection_reason"], "السعر أعلى من العرض")
        purchases.submit_purchase_order(self.db, order_id, 1)
        self.db.commit()
        resubmitted = self.db.execute(
            "SELECT status,rejection_reason,approved_by FROM purchase_orders WHERE id=?", (order_id,)
        ).fetchone()
        self.assertEqual(resubmitted["status"], "pending_approval")
        self.assertIsNone(resubmitted["rejection_reason"])
        self.assertIsNone(resubmitted["approved_by"])

    def test_supplier_bill_requires_independent_approval_before_posting(self):
        purchases.init_purchase_schema()
        self.db.execute("INSERT INTO suppliers(code,name) VALUES('SUP-BILL-APP','مورد فاتورة')")
        supplier_id = self.db.execute("SELECT id FROM suppliers WHERE code='SUP-BILL-APP'").fetchone()["id"]
        self.db.execute("INSERT INTO stock_warehouses(code,name_ar) VALUES('WB-APP','مخزن فاتورة')")
        warehouse_id = self.db.execute("SELECT id FROM stock_warehouses WHERE code='WB-APP'").fetchone()["id"]
        self.db.execute("""INSERT INTO goods_receipts(receipt_no,supplier_id,warehouse_id,receipt_date,total_value)
            VALUES('GR-BILL-APP',? ,?,'2026-11-13','100')""", (supplier_id, warehouse_id))
        receipt_id = self.db.execute("SELECT id FROM goods_receipts WHERE receipt_no='GR-BILL-APP'").fetchone()["id"]
        self.db.execute("INSERT INTO stock_products(id,sku,name_ar) VALUES(92,'BILL-APP-ITEM','صنف فاتورة الاعتماد')")
        self.db.execute("""INSERT INTO goods_receipt_lines(
            receipt_id,product_id,quantity,unit_cost,total_cost
        ) VALUES(?,92,'10','10','100')""", (receipt_id,))
        receipt_line_id = self.db.execute(
            "SELECT id FROM goods_receipt_lines WHERE receipt_id=?", (receipt_id,)
        ).fetchone()["id"]
        self.db.execute("""INSERT INTO supplier_bills(bill_no,supplier_id,bill_date,due_date,supplier_invoice_no,
            subtotal,tax_rate,tax_amount,total_amount,created_by,status,bill_kind)
            VALUES('BILL-APP',?,'2026-11-13','2026-12-13','INV-APP','100','0','0','100',10,'draft','inventory')""",
            (supplier_id,))
        bill_id = self.db.execute("SELECT id FROM supplier_bills WHERE bill_no='BILL-APP'").fetchone()["id"]
        self.db.execute("INSERT INTO supplier_bill_receipts(bill_id,receipt_id,matched_amount) VALUES(?,?,?)",
                        (bill_id, receipt_id, "100"))
        self.db.execute("""INSERT INTO supplier_bill_inventory_lines(
            bill_id,line_no,receipt_line_id,product_id,billed_quantity,invoice_unit_price,line_total
        ) VALUES(?,1,?,92,'10','10','100')""", (bill_id, receipt_line_id))
        self.db.commit()

        purchases.submit_supplier_bill(self.db, bill_id, 10)
        self.db.commit()
        self.assertEqual(self.db.execute("SELECT status FROM supplier_bills WHERE id=?", (bill_id,)).fetchone()[0],
                         "pending_approval")
        with self.assertRaisesRegex(ValueError, "لا يجوز اعتماد"):
            purchases.approve_supplier_bill(self.db, bill_id, 10)
        self.db.rollback()
        purchases.approve_supplier_bill(self.db, bill_id, 20, "مطابقة فاتورة المورد")
        self.db.commit()
        self.assertEqual(self.db.execute("SELECT status FROM supplier_bills WHERE id=?", (bill_id,)).fetchone()[0],
                         "approved")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0], 0)

        with patch("purchases.supplier_ap_account", return_value=99) as ap_patch, patch("purchases.create_journal", return_value=77) as journal_patch:
            entry_id = purchases.post_supplier_bill(self.db, bill_id, 30)
        ap_patch.assert_called_once_with(self.db)
        journal_patch.assert_called_once()
        journal_args = journal_patch.call_args.args
        self.assertIs(journal_args[0], self.db)
        self.assertEqual(journal_args[1], "2026-11-13")
        self.assertEqual(journal_args[2], "BILL-APP")
        self.db.commit()
        posted = self.db.execute("SELECT status,journal_entry_id FROM supplier_bills WHERE id=?", (bill_id,)).fetchone()
        self.assertEqual(posted["status"], "posted")
        self.assertEqual(posted["journal_entry_id"], entry_id)
        self.assertEqual(entry_id, 77)
        with self.assertRaisesRegex(ValueError, "مرحّلة بالفعل"):
            purchases.post_supplier_bill(self.db, bill_id, 30)
        self.db.rollback()

    def test_supplier_price_discount_clears_grni_and_reduces_remaining_stock_and_cogs(self):
        purchases.init_purchase_schema()
        self.db.execute(
            "INSERT INTO accounts(code,name_ar,account_type,is_group) VALUES('1150','مخزون','asset',0)"
        )
        self.db.execute(
            "INSERT INTO accounts(code,name_ar,account_type,is_group) VALUES('5100','تكلفة المبيعات','expense',0)"
        )
        self.db.execute("INSERT INTO suppliers(code,name) VALUES('SUP-PRICE','مورد خصم سعر')")
        supplier_id = self.db.execute(
            "SELECT id FROM suppliers WHERE code='SUP-PRICE'"
        ).fetchone()["id"]
        self.db.execute("INSERT INTO stock_warehouses(code,name_ar) VALUES('W-PRICE','مخزن الخصم')")
        warehouse_id = self.db.execute(
            "SELECT id FROM stock_warehouses WHERE code='W-PRICE'"
        ).fetchone()["id"]
        self.db.execute("INSERT INTO stock_products(id,sku,name_ar) VALUES(501,'PRICE-ITEM','صنف خصم سعر')")
        self.db.execute(
            """INSERT INTO goods_receipts(receipt_no,supplier_id,warehouse_id,receipt_date,total_value)
               VALUES('GR-PRICE',?,?,'2026-11-10','360')""",
            (supplier_id, warehouse_id),
        )
        receipt_id = self.db.execute(
            "SELECT id FROM goods_receipts WHERE receipt_no='GR-PRICE'"
        ).fetchone()["id"]
        self.db.execute(
            """INSERT INTO goods_receipt_lines(receipt_id,product_id,quantity,unit_cost,total_cost)
               VALUES(?,501,'10','36','360')""",
            (receipt_id,),
        )
        self.db.execute(
            """INSERT INTO stock_movements(
                 movement_no,movement_type,product_id,target_warehouse_id,quantity,unit_cost,
                 total_cost,movement_date,notes,accounting_status)
               VALUES('PUR-GR-PRICE','receipt',501,?,'10','36','360','2026-11-10',
                      'استلام مشتريات GR-PRICE','posted')""",
            (warehouse_id,),
        )
        movement_id = self.db.execute(
            "SELECT id FROM stock_movements WHERE movement_no='PUR-GR-PRICE'"
        ).fetchone()["id"]
        self.db.execute(
            """INSERT INTO stock_valuation_layers(
                 product_id,warehouse_id,source_movement_id,qty_in,qty_remaining,unit_cost)
               VALUES(501,?,?,'10','5','36')""",
            (warehouse_id, movement_id),
        )
        self.db.execute(
            """INSERT INTO supplier_bills(
                 bill_no,supplier_id,bill_date,due_date,supplier_invoice_no,subtotal,
                 tax_rate,tax_amount,total_amount,created_by,status,bill_kind)
               VALUES('BILL-PRICE',?,'2026-11-11','2026-12-11','INV-PRICE',
                      '340','0','0','340',1,'approved','inventory')""",
            (supplier_id,),
        )
        bill_id = self.db.execute(
            "SELECT id FROM supplier_bills WHERE bill_no='BILL-PRICE'"
        ).fetchone()["id"]
        self.db.execute(
            "INSERT INTO supplier_bill_receipts(bill_id,receipt_id,matched_amount) VALUES(?,?,?)",
            (bill_id, receipt_id, "360"),
        )
        self.db.commit()

        allocation = purchases._validate_supplier_receipt_allocations(
            self.db, supplier_id, [receipt_id], purchases.money("340"),
            {"receipt_amount_" + str(receipt_id): "360"}, exclude_bill_id=bill_id
        )
        self.assertEqual(allocation, [(receipt_id, purchases.money("360"))])

        with patch("purchases.supplier_ap_account", return_value=99), \
             patch("purchases._activity_posting_accounts", return_value={
                 "supplier_ap": "2110", "purchase_inventory": "1150", "cogs": "5100"
             }), \
             patch("activity_profiles.resolve_product_account_code",
                   side_effect=lambda _c, _p, role, default: "1150" if role == "inventory" else "5100"), \
             patch("purchases.create_journal", return_value=777) as journal:
            entry_id = purchases.post_supplier_bill(self.db, bill_id, 2)

        self.assertEqual(entry_id, 777)
        journal_lines = journal.call_args.args[4]
        self.assertEqual(
            [(line[0], Decimal(str(line[1])), Decimal(str(line[2]))) for line in journal_lines],
            [
                (self.db.execute("SELECT id FROM accounts WHERE code='2190'").fetchone()["id"],
                 Decimal("360.00"), Decimal("0")),
                (99, Decimal("0"), Decimal("340.00")),
                (self.db.execute("SELECT id FROM accounts WHERE code='1150'").fetchone()["id"],
                 Decimal("0"), Decimal("10.00")),
                (self.db.execute("SELECT id FROM accounts WHERE code='5100'").fetchone()["id"],
                 Decimal("0"), Decimal("10.00")),
            ],
        )
        layer = self.db.execute(
            "SELECT qty_remaining,unit_cost,value_remaining FROM stock_valuation_layers WHERE source_movement_id=?",
            (movement_id,),
        ).fetchone()
        self.assertEqual(Decimal(layer["qty_remaining"]), Decimal("5"))
        self.assertEqual(Decimal(layer["unit_cost"]), Decimal("34"))
        # The remaining five units must carry the discounted IAS 2 value,
        # not the pre-discount value left behind in value_remaining.
        self.assertEqual(Decimal(layer["value_remaining"]), Decimal("170.00"))
        movement = self.db.execute(
            "SELECT unit_cost,total_cost FROM stock_movements WHERE id=?", (movement_id,)
        ).fetchone()
        self.assertEqual((Decimal(movement["unit_cost"]), Decimal(movement["total_cost"])),
                         (Decimal("34"), Decimal("340")))
        receipt_line = self.db.execute(
            "SELECT unit_cost,total_cost FROM goods_receipt_lines WHERE receipt_id=?",
            (receipt_id,),
        ).fetchone()
        self.assertEqual((Decimal(receipt_line["unit_cost"]), Decimal(receipt_line["total_cost"])),
                         (Decimal("34"), Decimal("340")))
        self.assertEqual(
            purchases._supplier_receipt_available_amount(self.db, receipt_id),
            purchases.money("0"),
        )

    def test_asset_supplier_bill_posts_ppe_and_creates_linked_asset_record(self):
        purchases.init_purchase_schema()
        fixed_assets.initialize_assets_schema()
        self.db.execute("INSERT INTO suppliers(code,name) VALUES('SUP-ASSET','مورد أصول')")
        supplier_id = self.db.execute(
            "SELECT id FROM suppliers WHERE code='SUP-ASSET'"
        ).fetchone()["id"]
        asset_account = self.db.execute(
            "INSERT INTO accounts(code,name_ar,account_type,is_group) VALUES('1500','معدات','asset',0)"
        ).lastrowid
        accum_account = self.db.execute(
            "INSERT INTO accounts(code,name_ar,account_type,is_group) VALUES('1590','مجمع إهلاك معدات','asset',0)"
        ).lastrowid
        depreciation_expense = self.db.execute(
            "INSERT INTO accounts(code,name_ar,account_type,is_group) VALUES('6500','مصروف إهلاك','expense',0)"
        ).lastrowid
        input_vat = self.db.execute(
            "INSERT INTO accounts(code,name_ar,account_type,is_group) VALUES('1170','ضريبة مدخلات','asset',0)"
        ).lastrowid
        self.db.execute("""INSERT INTO supplier_bills(
            bill_no,supplier_id,bill_date,due_date,supplier_invoice_no,subtotal,
            tax_rate,tax_amount,total_amount,created_by,status,bill_kind,
            asset_code,asset_name,asset_category,asset_ready_date,asset_residual_value,
            asset_useful_life_months,asset_depreciation_method,asset_annual_rate,
            asset_account_id,asset_accum_account_id,asset_expense_account_id
        ) VALUES('BILL-ASSET-1',?,'2026-11-13','2026-12-13','INV-ASSET-1',
            '120000','15','18000','138000',10,'approved','asset',
            'AST-0001','ماكينة تعبئة','معدات','2026-11-20','12000',
            60,'straight_line','0',?,?,?)""",
            (supplier_id, asset_account, accum_account, depreciation_expense))
        bill_id = self.db.execute(
            "SELECT id FROM supplier_bills WHERE bill_no='BILL-ASSET-1'"
        ).fetchone()["id"]
        self.db.commit()

        with patch("purchases.supplier_ap_account", return_value=99), \
             patch("purchases._activity_tax_roles", return_value={"input_vat": "1170"}), \
             patch("purchases.create_journal", return_value=777) as journal:
            entry_id = purchases.post_supplier_bill(self.db, bill_id, 30)
        self.db.commit()

        self.assertEqual(entry_id, 777)
        bill = self.db.execute(
            "SELECT status,journal_entry_id FROM supplier_bills WHERE id=?", (bill_id,)
        ).fetchone()
        self.assertEqual((bill["status"], bill["journal_entry_id"]), ("posted", 777))
        asset = self.db.execute(
            """SELECT f.* FROM fixed_assets f
               LEFT JOIN fixed_asset_supplier_bill_links l ON l.asset_id=f.id
               WHERE f.supplier_bill_id=? OR l.bill_id=?""", (bill_id,bill_id)
        ).fetchone()
        self.assertIsNotNone(asset)
        self.assertEqual(asset["asset_code"], "AST-0001")
        self.assertEqual(asset["cost"], "120000")
        self.assertEqual(asset["purchase_date"], "2026-11-13")
        self.assertEqual(asset["ready_date"], "2026-11-20")
        self.assertEqual(asset["asset_account_id"] if "asset_account_id" in asset.keys()
                         else asset["asset_account_id"], asset_account)
        lines = journal.call_args.args[4]
        self.assertEqual(lines[0][0], asset_account)
        self.assertEqual(lines[0][1], purchases.money("120000"))
        self.assertEqual(lines[2][0], input_vat)
        self.assertEqual(lines[2][1], purchases.money("18000"))
        self.assertEqual(lines[3][0], 99)
        self.assertEqual(lines[3][2], purchases.money("18000"))
        total_debit = sum((purchases.money(line[1]) for line in lines), purchases.money("0"))
        total_credit = sum((purchases.money(line[2]) for line in lines), purchases.money("0"))
        self.assertEqual(total_debit, total_credit)

    def test_supplier_bill_creates_multiple_independent_assets_from_one_invoice(self):
        purchases.init_purchase_schema()
        fixed_assets.initialize_assets_schema()
        self.db.execute("INSERT INTO suppliers(code,name) VALUES('SUP-MULTI-ASSET','مورد أصول متعددة')")
        supplier_id = self.db.execute(
            "SELECT id FROM suppliers WHERE code='SUP-MULTI-ASSET'"
        ).fetchone()["id"]
        asset_account = self.db.execute(
            "INSERT INTO accounts(code,name_ar,account_type,is_group) VALUES('1520','أصول معدات','asset',0)"
        ).lastrowid
        accum_account = self.db.execute(
            "INSERT INTO accounts(code,name_ar,account_type,is_group) VALUES('1592','مجمع إهلاك ماكينة التعبئة','asset',0)"
        ).lastrowid
        accum_account_2 = self.db.execute(
            "INSERT INTO accounts(code,name_ar,account_type,is_group) VALUES('1593','مجمع إهلاك الرافعة الشوكية','asset',0)"
        ).lastrowid
        depreciation_expense = self.db.execute(
            "INSERT INTO accounts(code,name_ar,account_type,is_group) VALUES('6502','مصروف إهلاك أصول','expense',0)"
        ).lastrowid
        input_vat = self.db.execute(
            "INSERT INTO accounts(code,name_ar,account_type,is_group) VALUES('1172','ضريبة مدخلات أصول','asset',0)"
        ).lastrowid
        self.db.execute("""INSERT INTO supplier_bills(
            bill_no,supplier_id,bill_date,due_date,supplier_invoice_no,subtotal,
            tax_rate,tax_amount,total_amount,created_by,status,bill_kind
        ) VALUES('BILL-MULTI-ASSET',?,'2026-11-13','2026-12-13','INV-MULTI-ASSET',
            '120000','15','18000','138000',10,'approved','asset')""", (supplier_id,))
        bill_id = self.db.execute(
            "SELECT id FROM supplier_bills WHERE bill_no='BILL-MULTI-ASSET'"
        ).fetchone()["id"]
        for line_no, code, name, cost, residual, life, ready, line_accum_account in (
            (1, "AST-MULTI-01", "ماكينة تعبئة", "70000", "7000", 60, "2026-11-20", accum_account),
            (2, "AST-MULTI-02", "رافعة شوكية", "50000", "5000", 36, "2026-11-25", accum_account_2),
        ):
            self.db.execute("""INSERT INTO supplier_bill_asset_lines(
                bill_id,line_no,asset_code,asset_name,asset_category,cost,ready_date,
                residual_value,useful_life_months,depreciation_method,annual_rate,
                asset_account_id,accum_account_id,expense_account_id
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (bill_id,line_no,code,name,"معدات",cost,ready,residual,life,"straight_line","0",
             asset_account,line_accum_account,depreciation_expense))
        self.db.commit()
        with patch("purchases.supplier_ap_account", return_value=99), \
             patch("purchases._activity_tax_roles", return_value={"input_vat": "1172"}), \
             patch("purchases.create_journal", return_value=779) as journal:
            entry_id = purchases.post_supplier_bill(self.db, bill_id, 30)
        self.db.commit()

        self.assertEqual(entry_id, 779)
        assets = self.db.execute(
            """SELECT f.asset_code,f.cost,f.residual_value,f.useful_life_months,f.accum_account_id,l.bill_id supplier_bill_id
               FROM fixed_assets f JOIN fixed_asset_supplier_bill_links l ON l.asset_id=f.id
               WHERE l.bill_id=? ORDER BY f.asset_code""", (bill_id,)
        ).fetchall()
        self.assertEqual(len(assets), 2)
        self.assertEqual(len({x["accum_account_id"] for x in assets}), 2)
        self.assertEqual(
            [(x["asset_code"],x["cost"],x["residual_value"],x["useful_life_months"]) for x in assets],
            [("AST-MULTI-01","70000","7000",60),("AST-MULTI-02","50000","5000",36)]
        )
        lines = journal.call_args.args[4]
        self.assertEqual([line[1] for line in lines[:2]],
                         [purchases.money("70000"),purchases.money("50000")])
        self.assertEqual(lines[3][0], input_vat)
        debit = sum((purchases.money(line[1]) for line in lines), purchases.money("0"))
        credit = sum((purchases.money(line[2]) for line in lines), purchases.money("0"))
        self.assertEqual(debit, credit)

    def test_asset_supplier_bill_rejects_duplicate_asset_code_before_posting(self):
        purchases.init_purchase_schema()
        fixed_assets.initialize_assets_schema()
        self.db.execute("INSERT INTO suppliers(code,name) VALUES('SUP-ASSET-DUP','مورد أصول مكرر')")
        supplier_id = self.db.execute(
            "SELECT id FROM suppliers WHERE code='SUP-ASSET-DUP'"
        ).fetchone()["id"]
        asset_account = self.db.execute(
            "INSERT INTO accounts(code,name_ar,account_type,is_group) VALUES('1510','معدات مكررة','asset',0)"
        ).lastrowid
        accum_account = self.db.execute(
            "INSERT INTO accounts(code,name_ar,account_type,is_group) VALUES('1591','مجمع إهلاك معدات مكررة','asset',0)"
        ).lastrowid
        depreciation_expense = self.db.execute(
            "INSERT INTO accounts(code,name_ar,account_type,is_group) VALUES('6501','مصروف إهلاك مكرر','expense',0)"
        ).lastrowid
        self.db.execute("""INSERT INTO fixed_assets(
            asset_code,asset_name,purchase_date,ready_date,cost,residual_value,
            useful_life_months,asset_account_id,accum_account_id,expense_account_id
        ) VALUES('AST-DUP','أصل سابق','2026-01-01','2026-01-01','1000','0',12,?,?,?)""",
            (asset_account, accum_account, depreciation_expense))
        self.db.execute("""INSERT INTO supplier_bills(
            bill_no,supplier_id,bill_date,due_date,supplier_invoice_no,subtotal,
            tax_rate,tax_amount,total_amount,created_by,status,bill_kind,
            asset_code,asset_name,asset_ready_date,asset_residual_value,
            asset_useful_life_months,asset_depreciation_method,asset_annual_rate,
            asset_account_id,asset_accum_account_id,asset_expense_account_id
        ) VALUES('BILL-ASSET-DUP',?,'2026-11-13','2026-12-13','INV-ASSET-DUP',
            '1200','0','0','1200',10,'approved','asset',
            'AST-DUP','أصل مكرر','2026-11-13','0',12,'straight_line','0',?,?,?)""",
            (supplier_id, asset_account, accum_account, depreciation_expense))
        bill_id = self.db.execute(
            "SELECT id FROM supplier_bills WHERE bill_no='BILL-ASSET-DUP'"
        ).fetchone()["id"]
        self.db.commit()
        with patch("purchases.supplier_ap_account", return_value=99), \
             patch("purchases.create_journal", return_value=778):
            with self.assertRaisesRegex(ValueError, "كود الأصل مستخدم"):
                purchases.post_supplier_bill(self.db, bill_id, 30)
        self.db.rollback()
        bill = self.db.execute(
            "SELECT status,journal_entry_id FROM supplier_bills WHERE id=?", (bill_id,)
        ).fetchone()
        self.assertEqual(bill["status"], "approved")
        self.assertIsNone(bill["journal_entry_id"])

    def test_supplier_bill_rejection_requires_reason_and_can_be_resubmitted(self):
        purchases.init_purchase_schema()
        self.db.execute("INSERT INTO suppliers(code,name) VALUES('SUP-BILL-REJ','مورد رفض فاتورة')")
        supplier_id = self.db.execute("SELECT id FROM suppliers WHERE code='SUP-BILL-REJ'").fetchone()["id"]
        self.db.execute("""INSERT INTO supplier_bills(bill_no,supplier_id,bill_date,due_date,supplier_invoice_no,
            subtotal,tax_rate,tax_amount,total_amount,created_by,status,bill_kind,expense_account_id)
            VALUES('BILL-REJ',?,'2026-11-13','2026-12-13','INV-REJ','50','0','0','50',10,'draft','expense',?)""",
            (supplier_id, self.db.execute("SELECT id FROM accounts WHERE code='5000'").fetchone()["id"]))
        bill_id = self.db.execute("SELECT id FROM supplier_bills WHERE bill_no='BILL-REJ'").fetchone()["id"]
        self.db.commit()
        purchases.submit_supplier_bill(self.db, bill_id, 10)
        self.db.commit()
        with self.assertRaisesRegex(ValueError, "سبب رفض"):
            purchases.reject_supplier_bill(self.db, bill_id, 20, "لا")
        self.db.rollback()
        purchases.reject_supplier_bill(self.db, bill_id, 20, "المستند يحتاج مراجعة")
        self.db.commit()
        state = self.db.execute("SELECT status,rejection_reason FROM supplier_bills WHERE id=?", (bill_id,)).fetchone()
        self.assertEqual(state["status"], "rejected")
        self.assertEqual(state["rejection_reason"], "المستند يحتاج مراجعة")
        purchases.submit_supplier_bill(self.db, bill_id, 10)
        self.db.commit()
        state = self.db.execute("SELECT status,rejection_reason,approved_by FROM supplier_bills WHERE id=?", (bill_id,)).fetchone()
        self.assertEqual(state["status"], "pending_approval")
        self.assertIsNone(state["rejection_reason"])
        self.assertIsNone(state["approved_by"])

    def test_restaurant_goods_receipt_posts_inventory_by_product_category(self):
        purchases.init_purchase_schema()
        self.db.execute("INSERT INTO company_settings(setting_key,setting_value) VALUES('activity','restaurant')")
        self.db.executemany(
            "INSERT INTO accounts(code,name_ar,account_type,is_group,active) VALUES(?,?,?,0,1)",
            [
                ("1140", "مخزون الغذاء", "asset"),
                ("1150", "مخزون المشروبات", "asset"),
                ("1160", "مخزون التعبئة", "asset"),
            ],
        )
        supplier_id = self.db.execute(
            "INSERT INTO suppliers(code,name) VALUES('SUP-REST','مورد مطعم')"
        ).lastrowid
        warehouse_id = self.db.execute(
            "INSERT INTO stock_warehouses(code,name_ar) VALUES('W-REST','مخزن المطعم')"
        ).lastrowid
        category_rows = [
            ("FOOD", "غذاء", "1140"),
            ("BEVERAGE", "مشروبات", "1150"),
            ("PACKAGING", "تعبئة", "1160"),
        ]
        products = []
        for code, name, expected_inventory in category_rows:
            category_id = self.db.execute(
                "INSERT INTO stock_categories(code,name_ar) VALUES(?,?)", (code, name)
            ).lastrowid
            product_id = self.db.execute(
                "INSERT INTO stock_products(sku,name_ar,category_id) VALUES(?,?,?)",
                ("PUR-" + code, name, category_id),
            ).lastrowid
            products.append((product_id, expected_inventory))

        app = Flask(__name__)
        app.secret_key = "test"
        app.add_url_rule("/__test_global_search", endpoint="global_search", view_func=lambda: "")
        app.register_blueprint(purchases.purchase_bp)
        client = app.test_client()
        with client.session_transaction() as session:
            session["user_id"] = 1
            session["role"] = "admin"

        for index, (product_id, expected_inventory) in enumerate(products, 1):
            response = client.post("/purchases/receipts/new", data={
                "supplier_id": str(supplier_id),
                "product_id": str(product_id),
                "warehouse_id": str(warehouse_id),
                "receipt_date": "2026-11-14",
                "quantity": "2",
                "unit_cost": "10",
            })
            self.assertEqual(response.status_code, 302)
            receipt = self.db.execute(
                "SELECT journal_entry_id FROM goods_receipts ORDER BY id DESC LIMIT 1"
            ).fetchone()
            self.assertIsNotNone(receipt["journal_entry_id"])
            lines = {
                row["code"]: (Decimal(row["debit"]), Decimal(row["credit"]))
                for row in self.db.execute(
                    """SELECT a.code,jl.debit,jl.credit FROM journal_lines jl
                       JOIN accounts a ON a.id=jl.account_id WHERE jl.entry_id=?""",
                    (receipt["journal_entry_id"],),
                )
            }
            self.assertEqual(lines[expected_inventory], (Decimal("20.00"), Decimal("0")))
            self.assertEqual(lines["2190"], (Decimal("0"), Decimal("20.00")))
            movement = self.db.execute(
                "SELECT accounting_status,journal_entry_id FROM stock_movements WHERE movement_no LIKE 'PUR-%' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            self.assertEqual(movement["accounting_status"], "posted")
            self.assertEqual(movement["journal_entry_id"], receipt["journal_entry_id"])

    def test_industrial_goods_receipt_posts_only_to_1130(self):
        purchases.init_purchase_schema()
        self.db.execute("INSERT INTO company_settings(setting_key,setting_value) VALUES('activity','industrial')")
        self.db.execute(
            "INSERT INTO accounts(code,name_ar,account_type,is_group,active) VALUES('1130','مخزون المواد والأصناف','asset',0,1)"
        )
        supplier_id = self.db.execute(
            "INSERT INTO suppliers(code,name) VALUES('SUP-IND','مورد صناعي')"
        ).lastrowid
        warehouse_id = self.db.execute(
            "INSERT INTO stock_warehouses(code,name_ar) VALUES('W-IND','مخزن صناعي')"
        ).lastrowid
        product_id = self.db.execute(
            "INSERT INTO stock_products(sku,name_ar) VALUES('IND-GOODS-1','بضاعة صناعية')"
        ).lastrowid

        app = Flask(__name__)
        app.secret_key = "test"
        app.add_url_rule("/__test_global_search", endpoint="global_search", view_func=lambda: "")
        app.register_blueprint(purchases.purchase_bp)
        client = app.test_client()
        with client.session_transaction() as session:
            session["user_id"] = 1
            session["role"] = "admin"

        response = client.post("/purchases/receipts/new", data={
            "supplier_id": str(supplier_id), "product_id": str(product_id),
            "warehouse_id": str(warehouse_id), "receipt_date": "2026-11-14",
            "quantity": "2", "unit_cost": "10",
        })
        self.assertEqual(response.status_code, 302)
        receipt = self.db.execute(
            "SELECT journal_entry_id FROM goods_receipts ORDER BY id DESC LIMIT 1"
        ).fetchone()
        lines = {
            row["code"]: (Decimal(row["debit"]), Decimal(row["credit"]))
            for row in self.db.execute(
                """SELECT a.code,jl.debit,jl.credit FROM journal_lines jl
                   JOIN accounts a ON a.id=jl.account_id WHERE jl.entry_id=?""",
                (receipt["journal_entry_id"],),
            )
        }
        self.assertEqual(lines["1130"], (Decimal("20.00"), Decimal("0")))
        self.assertEqual(lines["2190"], (Decimal("0"), Decimal("20.00")))
        self.assertNotIn("1140", lines)
        self.assertNotIn("1150", lines)

    def test_journal_posting_is_balanced(self):
        dr = purchases.account(self.db, "5000", "expense")
        cr = purchases.account(self.db, "2190", "liability")
        entry_id = purchases.create_journal(
            self.db, "2026-11-09", "TEST-1", "اختبار قيد",
            [(dr, purchases.money("125.50"), 0, "مدين"),
             (cr, 0, purchases.money("125.50"), "دائن")], 1)
        from decimal import Decimal
        rows = self.db.execute(
            "SELECT debit,credit FROM journal_lines WHERE entry_id=?", (entry_id,)
        ).fetchall()
        debit = sum((Decimal(row["debit"]) for row in rows), Decimal("0"))
        credit = sum((Decimal(row["credit"]) for row in rows), Decimal("0"))
        self.assertEqual(debit, credit)

    def test_integrity_resolve_button_only_closes_after_duplicate_is_removed(self):
        self.db.executescript("""
          CREATE TABLE purchase_integrity_issues(
            id INTEGER PRIMARY KEY, issue_type TEXT, issue_key TEXT,
            detected_at TEXT DEFAULT CURRENT_TIMESTAMP, resolved INTEGER DEFAULT 0,
            resolved_by INTEGER, resolved_at TEXT
          );
          CREATE TABLE supplier_bills(
            id INTEGER PRIMARY KEY, supplier_id INTEGER, supplier_invoice_no TEXT
          );
          INSERT INTO purchase_integrity_issues(id,issue_type,issue_key)
            VALUES(1,'duplicate_supplier_invoice','1:inv-100');
          INSERT INTO supplier_bills VALUES(1,1,'INV-100');
          INSERT INTO supplier_bills VALUES(2,1,' inv-100 ');
        """)
        app = Flask(__name__)
        app.add_url_rule("/__test_global_search", endpoint="global_search", view_func=lambda: "")
        app.secret_key = "test"
        app.register_blueprint(purchases.purchase_bp)
        client = app.test_client()
        with client.session_transaction() as session:
            session["user_id"] = 1
            session["role"] = "admin"
        response = client.post("/purchases/integrity/1/resolve")
        self.assertEqual(response.status_code, 302)
        state = self.db.execute("SELECT resolved FROM purchase_integrity_issues WHERE id=1").fetchone()[0]
        self.assertEqual(state, 0)
        self.db.execute("DELETE FROM supplier_bills WHERE id=2")
        response = client.post("/purchases/integrity/1/resolve")
        self.assertEqual(response.status_code, 302)
        issue = self.db.execute(
            "SELECT resolved,resolved_by,resolved_at FROM purchase_integrity_issues WHERE id=1"
        ).fetchone()
        self.assertEqual(issue["resolved"], 1)
        self.assertEqual(issue["resolved_by"], 1)
        self.assertTrue(issue["resolved_at"])

    def test_group_account_cannot_be_posted(self):
        with self.assertRaises(ValueError):
            purchases.account(self.db, "2000", "liability")

    def test_detail_account_setup_rejects_automatic_account_creation(self):
        before = self.db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
        with self.assertRaisesRegex(ValueError, "غير مضبوط"):
            purchases.ensure_detail_account(
                self.db, "2191", "GRNI فرعي", "liability", "2000")
        after = self.db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
        self.assertEqual(after, before)


    def test_partial_receipt_allocations_reserve_only_remaining_value(self):
        purchases.init_purchase_schema()
        self.db.execute("CREATE UNIQUE INDEX uq_supplier_bill_receipt_once ON supplier_bill_receipts(receipt_id)")
        purchases.init_purchase_schema()
        indexes = {row["name"] for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertNotIn("uq_supplier_bill_receipt_once", indexes)
        self.db.execute("INSERT INTO suppliers(code,name) VALUES('SUP-PART','مورد جزئي')")
        supplier_id = self.db.execute("SELECT id FROM suppliers WHERE code='SUP-PART'").fetchone()["id"]
        self.db.execute("INSERT INTO stock_warehouses(code,name_ar) VALUES('W-PART','مخزن جزئي')")
        warehouse_id = self.db.execute("SELECT id FROM stock_warehouses WHERE code='W-PART'").fetchone()["id"]
        self.db.execute("""INSERT INTO goods_receipts(receipt_no,supplier_id,warehouse_id,receipt_date,total_value)
            VALUES('GR-PART',?,?,'2026-11-13','100')""", (supplier_id, warehouse_id))
        receipt_id = self.db.execute("SELECT id FROM goods_receipts WHERE receipt_no='GR-PART'").fetchone()["id"]
        self.db.execute("""INSERT INTO supplier_bills(bill_no,supplier_id,bill_date,supplier_invoice_no,
            subtotal,tax_rate,tax_amount,total_amount,status)
            VALUES('BILL-PART-1',?,'2026-11-13','INV-PART-1','40','0','0','40','draft')""", (supplier_id,))
        first_bill = self.db.execute("SELECT id FROM supplier_bills WHERE bill_no='BILL-PART-1'").fetchone()["id"]
        self.db.execute("INSERT INTO supplier_bill_receipts(bill_id,receipt_id,matched_amount) VALUES(?,?,?)",
                        (first_bill, receipt_id, "40"))
        self.db.commit()

        available = purchases._supplier_receipt_available_amount(self.db, receipt_id)
        self.assertEqual(available, purchases.money("60"))
        allocations = purchases._validate_supplier_receipt_allocations(
            self.db, supplier_id, [receipt_id], purchases.money("60"),
            {"receipt_amount_" + str(receipt_id): "60"}
        )
        self.assertEqual(allocations, [(receipt_id, purchases.money("60"))])
        with self.assertRaisesRegex(ValueError, "قيمة التخصيص تتجاوز المتبقي غير المفوتر من إيصال الاستلام"):
            purchases._validate_supplier_receipt_allocations(
                self.db, supplier_id, [receipt_id], purchases.money("61"),
                {"receipt_amount_" + str(receipt_id): "61"}
            )


    def test_supplier_bill_submission_rejects_grni_allocation_mismatch(self):
        purchases.init_purchase_schema()
        self.db.execute("INSERT INTO suppliers(code,name) VALUES('SUP-SUBMIT-GRNI','مورد مراجعة')")
        supplier_id = self.db.execute(
            "SELECT id FROM suppliers WHERE code='SUP-SUBMIT-GRNI'"
        ).fetchone()["id"]
        self.db.execute("INSERT INTO stock_warehouses(code,name_ar) VALUES('WH-SUBMIT-GRNI','مخزن مراجعة')")
        warehouse_id = self.db.execute(
            "SELECT id FROM stock_warehouses WHERE code='WH-SUBMIT-GRNI'"
        ).fetchone()["id"]
        self.db.execute("""INSERT INTO goods_receipts(
            receipt_no,supplier_id,warehouse_id,receipt_date,status,total_value
        ) VALUES('GR-SUBMIT-GRNI',?,?,'2026-11-13','posted','100.00')""",
            (supplier_id, warehouse_id))
        receipt_id = self.db.execute(
            "SELECT id FROM goods_receipts WHERE receipt_no='GR-SUBMIT-GRNI'"
        ).fetchone()["id"]
        self.db.execute("""INSERT INTO supplier_bills(
            bill_no,supplier_id,bill_date,due_date,supplier_invoice_no,subtotal,
            tax_rate,tax_amount,total_amount,status,bill_kind
        ) VALUES('BILL-SUBMIT-GRNI',?,'2026-11-13','2026-11-30','INV-SUBMIT-GRNI',
            '100.00','0','0','100.00','draft','inventory')""", (supplier_id,))
        bill_id = self.db.execute(
            "SELECT id FROM supplier_bills WHERE bill_no='BILL-SUBMIT-GRNI'"
        ).fetchone()["id"]
        self.db.execute(
            "INSERT INTO supplier_bill_receipts(bill_id,receipt_id,matched_amount) VALUES(?,?,?)",
            (bill_id, receipt_id, "40.00"),
        )
        self.db.commit()

        with self.assertRaisesRegex(ValueError, "مجموع تخصيصات إيصالات GRNI"):
            purchases.submit_supplier_bill(self.db, bill_id, 10)
        self.db.rollback()
        bill = self.db.execute(
            "SELECT status,journal_entry_id FROM supplier_bills WHERE id=?", (bill_id,)
        ).fetchone()
        self.assertEqual(bill["status"], "draft")
        self.assertIsNone(bill["journal_entry_id"])


    def test_supplier_return_package_conversion_respects_purchase_unit_permission(self):
        base_unit = self.db.execute(
            "SELECT id FROM stock_uoms WHERE code='PCS'"
        ).fetchone()["id"]
        product_id = self.db.execute(
            "INSERT INTO stock_products(sku,name_ar,base_uom_id) VALUES('RET-UOM-1','صنف مرتجع',?)",
            (base_unit,),
        ).lastrowid
        package_id = self.db.execute(
            """INSERT INTO stock_product_uoms(
                 product_id,custom_code,custom_name,factor_to_base,is_base,
                 purchase_enabled,sales_enabled)
               VALUES(?,?,?,?,0,1,0)""",
            (product_id, "CASE12", "كرتونة 12 قطعة", "12"),
        ).lastrowid

        factor, unit_name = purchases.product_uom_factor(
            self.db, product_id, "P:" + str(package_id), "purchase"
        )
        stored_unit_id, stored_name = purchases.product_uom_storage(
            self.db, product_id, "P:" + str(package_id)
        )
        self.assertEqual(factor, Decimal("12"))
        self.assertEqual(unit_name, "كرتونة 12 قطعة")
        self.assertIsNone(stored_unit_id)
        self.assertEqual(stored_name, "كرتونة 12 قطعة")

        self.db.execute(
            "UPDATE stock_product_uoms SET purchase_enabled=0 WHERE id=?", (package_id,)
        )
        with self.assertRaisesRegex(ValueError, "غير مفعلة للمشتريات"):
            purchases.product_uom_factor(
                self.db, product_id, "P:" + str(package_id), "purchase"
            )


    def test_supplier_invoice_lines_match_receipt_quantities_and_block_duplicate_billing(self):
        from werkzeug.datastructures import MultiDict
        purchases.init_purchase_schema()
        self.db.execute("INSERT INTO suppliers(code,name) VALUES('SUP-MATCH','مورد المطابقة')")
        supplier_id = self.db.execute("SELECT id FROM suppliers WHERE code='SUP-MATCH'").fetchone()["id"]
        self.db.execute("INSERT INTO stock_warehouses(code,name_ar) VALUES('W-MATCH','مخزن المطابقة')")
        warehouse_id = self.db.execute("SELECT id FROM stock_warehouses WHERE code='W-MATCH'").fetchone()["id"]
        self.db.execute("INSERT INTO stock_products(id,sku,name_ar) VALUES(91,'MATCH-ITEM','صنف المطابقة')")
        self.db.execute("""INSERT INTO purchase_orders(po_no,supplier_id,order_date,status,created_by)
            VALUES('PO-MATCH',?,'2026-11-14','approved',2)""", (supplier_id,))
        po_id = self.db.execute("SELECT id FROM purchase_orders WHERE po_no='PO-MATCH'").fetchone()["id"]
        self.db.execute("""INSERT INTO purchase_order_lines(po_id,product_id,quantity,unit_price)
            VALUES(?,91,'5','10')""", (po_id,))
        po_line_id = self.db.execute("SELECT id FROM purchase_order_lines WHERE po_id=?", (po_id,)).fetchone()["id"]
        self.db.execute("""INSERT INTO goods_receipts(receipt_no,supplier_id,warehouse_id,receipt_date,
            status,total_value) VALUES('GR-MATCH',?,?,'2026-11-14','posted','30')""",
            (supplier_id, warehouse_id))
        receipt_id = self.db.execute("SELECT id FROM goods_receipts WHERE receipt_no='GR-MATCH'").fetchone()["id"]
        self.db.execute("""INSERT INTO goods_receipt_lines(receipt_id,po_line_id,product_id,quantity,unit_cost,total_cost)
            VALUES(?,?,91,'3','10','30')""", (receipt_id, po_line_id))
        receipt_line_id = self.db.execute(
            "SELECT id FROM goods_receipt_lines WHERE receipt_id=?", (receipt_id,)
        ).fetchone()["id"]
        self.db.commit()

        form = MultiDict({
            "invoice_quantity_" + str(receipt_line_id): "2",
            "invoice_unit_price_" + str(receipt_line_id): "10"
        })
        lines = purchases._validate_supplier_bill_inventory_lines(
            self.db, supplier_id, [receipt_id], purchases.money("20"), form
        )
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["line_total"], purchases.money("20"))

        self.db.execute("""INSERT INTO supplier_bills(bill_no,supplier_id,bill_date,supplier_invoice_no,
            subtotal,tax_rate,tax_amount,total_amount,status)
            VALUES('BILL-MATCH',?,'2026-11-14','INV-MATCH','20','0','0','20','draft')""",
            (supplier_id,))
        bill_id = self.db.execute("SELECT id FROM supplier_bills WHERE bill_no='BILL-MATCH'").fetchone()["id"]
        self.db.execute("""INSERT INTO supplier_bill_inventory_lines(
            bill_id,line_no,receipt_line_id,product_id,billed_quantity,invoice_unit_price,line_total
        ) VALUES(?,1,?,91,'2','10','20')""", (bill_id, receipt_line_id))
        self.db.commit()

        with self.assertRaisesRegex(ValueError, "تتجاوز الكمية المستلمة"):
            purchases._validate_supplier_bill_inventory_lines(
                self.db, supplier_id, [receipt_id], purchases.money("20"), form
            )
        with self.assertRaisesRegex(ValueError, "مجموع بنود فاتورة المورد"):
            purchases._validate_supplier_bill_inventory_lines(
                self.db, supplier_id, [receipt_id], purchases.money("11"),
                MultiDict({
                    "invoice_quantity_" + str(receipt_line_id): "1",
                    "invoice_unit_price_" + str(receipt_line_id): "10"
                })
            )


if __name__ == "__main__":
    unittest.main()
