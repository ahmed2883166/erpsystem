"""NEXUS ERP inventory foundation (v0.2).

Implements product master, warehouses, stock receipts/issues/transfers/scrap,
lot/expiry tracking, non-negative stock validation and valuation layers.
Manufacturing orders and production conversion are intentionally excluded.
"""
import sqlite3
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import wraps
from pathlib import Path
from datetime import date
from flask import Blueprint, current_app, flash, redirect, render_template, request, session, url_for
from accounting_core import post_journal

stock_bp = Blueprint("stock", __name__, url_prefix="/stock")
DB_PATH = Path(__file__).resolve().parent / "data" / "nexus.db"
D = Decimal
QTY = D("0.0001")
MONEY = D("0.01")


def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=20, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=20000")
    return conn


def initialize_stock_schema():
    DB_PATH.parent.mkdir(exist_ok=True)
    c = get_db()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS stock_products(
      id INTEGER PRIMARY KEY, sku TEXT NOT NULL UNIQUE, name_ar TEXT NOT NULL,
      category TEXT NOT NULL DEFAULT '', product_type TEXT NOT NULL DEFAULT 'goods',
      uom TEXT NOT NULL DEFAULT 'قطعة', barcode TEXT UNIQUE, track_lot INTEGER NOT NULL DEFAULT 0,
      track_expiry INTEGER NOT NULL DEFAULT 0, min_qty TEXT NOT NULL DEFAULT '0',
      active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS stock_warehouses(
      id INTEGER PRIMARY KEY, code TEXT NOT NULL UNIQUE, name_ar TEXT NOT NULL,
      branch TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS stock_lots(
      id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL REFERENCES stock_products(id),
      lot_number TEXT NOT NULL, production_date TEXT, expiry_date TEXT,
      UNIQUE(product_id,lot_number)
    );
    CREATE TABLE IF NOT EXISTS stock_movements(
      id INTEGER PRIMARY KEY, movement_no TEXT NOT NULL UNIQUE, movement_type TEXT NOT NULL,
      product_id INTEGER NOT NULL REFERENCES stock_products(id),
      source_warehouse_id INTEGER REFERENCES stock_warehouses(id),
      target_warehouse_id INTEGER REFERENCES stock_warehouses(id),
      lot_id INTEGER REFERENCES stock_lots(id), quantity TEXT NOT NULL,
      unit_cost TEXT NOT NULL DEFAULT '0', total_cost TEXT NOT NULL DEFAULT '0',
      movement_date TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '',
      created_by INTEGER, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS stock_valuation_layers(
      id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL REFERENCES stock_products(id),
      warehouse_id INTEGER NOT NULL REFERENCES stock_warehouses(id),
      lot_id INTEGER REFERENCES stock_lots(id), source_movement_id INTEGER NOT NULL REFERENCES stock_movements(id),
      qty_in TEXT NOT NULL, qty_remaining TEXT NOT NULL, unit_cost TEXT NOT NULL,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_stock_moves_product_date ON stock_movements(product_id,movement_date,id);
    CREATE INDEX IF NOT EXISTS idx_stock_layers_product_wh ON stock_valuation_layers(product_id,warehouse_id,id);
    """)
    # Accounting link and state are additive migrations for existing databases.
    movement_columns = {row["name"] for row in c.execute("PRAGMA table_info(stock_movements)")}
    if "journal_entry_id" not in movement_columns:
        c.execute("ALTER TABLE stock_movements ADD COLUMN journal_entry_id INTEGER")
    if "accounting_status" not in movement_columns:
        c.execute("ALTER TABLE stock_movements ADD COLUMN accounting_status TEXT NOT NULL DEFAULT 'pending'")
    if "issue_purpose" not in movement_columns:
        c.execute("ALTER TABLE stock_movements ADD COLUMN issue_purpose TEXT")
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_stock_movement_journal ON stock_movements(journal_entry_id) WHERE journal_entry_id IS NOT NULL")
    c.close()


def post_scrap_journal(c, movement_id, movement_no, product, movement_date, total_cost, created_by):
    """Post an approved scrap movement only when explicit chart accounts exist."""
    existing = c.execute("SELECT journal_entry_id FROM stock_movements WHERE id=?", (movement_id,)).fetchone()
    if existing and existing["journal_entry_id"]:
        return existing["journal_entry_id"]
    settings = {row["setting_key"]: row["setting_value"] for row in c.execute(
        "SELECT setting_key,setting_value FROM company_settings WHERE setting_key IN ('activity',)") }
    activity = settings.get("activity", "commercial")
    inventory_codes = {
        "commercial": "1150", "industrial": "1130", "medical": "1150",
        "restaurant": "1140", "service": None,
    }
    inventory_code = inventory_codes.get(activity)
    if not inventory_code:
        c.execute("UPDATE stock_movements SET accounting_status='pending_setup' WHERE id=?", (movement_id,))
        return None
    inventory = c.execute("SELECT id,is_group,active FROM accounts WHERE code=?", (inventory_code,)).fetchone()
    # Create a dedicated write-off expense under the activity's expense group when possible.
    writeoff = c.execute("SELECT id,is_group,active FROM accounts WHERE code='6990'").fetchone()
    if not writeoff:
        parent = c.execute("SELECT id FROM accounts WHERE code='6000' AND is_group=1").fetchone()
        if not parent:
            parent = c.execute("SELECT id FROM accounts WHERE account_type='expense' AND is_group=1 ORDER BY code LIMIT 1").fetchone()
        if parent:
            cur = c.execute("INSERT INTO accounts(code,name_ar,account_type,parent_id,is_group,active) VALUES('6990','خسائر تالف المخزون','expense',?,0,1)", (parent["id"],))
            writeoff_id = cur.lastrowid
        else:
            writeoff_id = None
    else:
        writeoff_id = writeoff["id"] if not writeoff["is_group"] and writeoff["active"] else None
    if not inventory or inventory["is_group"] or not inventory["active"] or not writeoff_id:
        c.execute("UPDATE stock_movements SET accounting_status='pending_setup' WHERE id=?", (movement_id,))
        return None
    amount = money(D(str(total_cost)))
    if amount <= 0:
        c.execute("UPDATE stock_movements SET accounting_status='not_required' WHERE id=?", (movement_id,))
        return None
    entry_id = post_journal(c,movement_date,movement_no,
        f"قيد آلي لإعدام مخزون - {product['name_ar']}",
        [(writeoff_id,amount,0,"خسائر تالف مخزون"),
         (inventory["id"],0,amount,"تخفيض قيمة المخزون")],created_by)
    c.execute("UPDATE stock_movements SET journal_entry_id=?,accounting_status='posted' WHERE id=?", (entry_id,movement_id))
    return entry_id


def post_issue_journal(c, movement_id, movement_no, product, movement_date, total_cost, created_by, purpose):
    """Create the GL entry for an issue using the valuation cost consumed in this transaction."""
    existing = c.execute("SELECT journal_entry_id FROM stock_movements WHERE id=?", (movement_id,)).fetchone()
    if existing and existing["journal_entry_id"]:
        return existing["journal_entry_id"]
    settings = {row["setting_key"]: row["setting_value"] for row in c.execute(
        "SELECT setting_key,setting_value FROM company_settings WHERE setting_key='activity'")}
    activity = settings.get("activity", "commercial")
    cogs_codes = {"commercial":"5100", "medical":"5300", "restaurant":"5100", "industrial":"5400"}
    if purpose == "sale":
        expense_code = cogs_codes.get(activity)
        expense_name = "تكلفة المبيعات / تكلفة الخدمات المصروفة"
    elif purpose == "internal":
        expense_code = "6991"
        expense_name = "مصروف استخدام داخلي من المخزون"
        existing_expense = c.execute("SELECT id,is_group,active FROM accounts WHERE code=?", (expense_code,)).fetchone()
        if not existing_expense:
            parent = c.execute("SELECT id FROM accounts WHERE code='6000' AND is_group=1").fetchone()
            if not parent:
                parent = c.execute("SELECT id FROM accounts WHERE account_type='expense' AND is_group=1 ORDER BY code LIMIT 1").fetchone()
            if parent:
                cur = c.execute("INSERT INTO accounts(code,name_ar,account_type,parent_id,is_group,active) VALUES(?,?,?,?,0,1)",
                                (expense_code, expense_name, "expense", parent["id"]))
                expense_id = cur.lastrowid
            else:
                expense_id = None
        else:
            expense_id = existing_expense["id"] if not existing_expense["is_group"] and existing_expense["active"] else None
    else:
        c.execute("UPDATE stock_movements SET accounting_status='pending_purpose' WHERE id=?", (movement_id,))
        return None
    if purpose == "sale":
        expense = c.execute("SELECT id,is_group,active FROM accounts WHERE code=?", (expense_code,)).fetchone() if expense_code else None
        expense_id = expense["id"] if expense and not expense["is_group"] and expense["active"] else None
    inventory_codes = {"commercial":"1150", "industrial":"1130", "medical":"1150", "restaurant":"1140"}
    inventory_code = inventory_codes.get(activity)
    inventory = c.execute("SELECT id,is_group,active FROM accounts WHERE code=?", (inventory_code,)).fetchone() if inventory_code else None
    if not expense_id or not inventory or inventory["is_group"] or not inventory["active"]:
        c.execute("UPDATE stock_movements SET accounting_status='pending_setup' WHERE id=?", (movement_id,))
        return None
    amount = money(D(str(total_cost)))
    if amount <= 0:
        c.execute("UPDATE stock_movements SET accounting_status='not_required' WHERE id=?", (movement_id,))
        return None
    entry_id = post_journal(c,movement_date,movement_no,
        f"قيد آلي لصرف مخزون - {product['name_ar']}",
        [(expense_id,amount,0,expense_name),
         (inventory["id"],0,amount,"تخفيض المخزون بتكلفة الصرف")],created_by)
    c.execute("UPDATE stock_movements SET journal_entry_id=?,accounting_status='posted' WHERE id=?", (entry_id, movement_id))
    return entry_id


def stock_permission(view_name):
    def deco(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            if "user_id" not in session:
                return redirect(url_for("login"))
            role = session.get("role")
            allowed = role == "admin" or (role == "accountant" and view_name in {"stock.manage","stock.view"}) or (role == "viewer" and view_name == "stock.view")
            if not allowed:
                flash("ليس لديك صلاحية لهذه العملية.", "error")
                return redirect(url_for("dashboard"))
            return fn(*args, **kwargs)
        return wrapped
    return deco


def dec(value, label):
    try:
        n = D(str(value))
        if not n.is_finite():
            raise InvalidOperation()
        return n
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f"قيمة {label} غير صحيحة.")


def q4(n):
    return n.quantize(QTY, rounding=ROUND_HALF_UP)


def money(n):
    return n.quantize(MONEY, rounding=ROUND_HALF_UP)


def current_qty(c, product_id, warehouse_id, lot_id=None):
    sql = """SELECT COALESCE(SUM(CAST(quantity AS REAL) *
      CASE WHEN movement_type='receipt' THEN 1 WHEN movement_type IN ('issue','scrap') THEN -1
      WHEN movement_type='transfer' AND target_warehouse_id=? THEN 1
      WHEN movement_type='transfer' AND source_warehouse_id=? THEN -1 ELSE 0 END),0) q
      FROM stock_movements WHERE product_id=?"""
    row = c.execute(sql, (warehouse_id, warehouse_id, product_id)).fetchone()
    return D(str(row["q"] or 0))


def available_layers(c, product_id, warehouse_id, lot_id=None):
    sql = """SELECT * FROM stock_valuation_layers WHERE product_id=? AND warehouse_id=?
             AND CAST(qty_remaining AS REAL)>0"""
    args = [product_id, warehouse_id]
    if lot_id:
        sql += " AND lot_id=?"; args.append(lot_id)
    return c.execute(sql + " ORDER BY id", args).fetchall()


def consume_layers(c, product_id, warehouse_id, qty, valuation_method, lot_id=None):
    layers = available_layers(c, product_id, warehouse_id, lot_id)
    available = sum((D(x["qty_remaining"]) for x in layers), D("0"))
    if available < qty:
        raise ValueError(f"الرصيد غير كافٍ. المتاح {q4(available)} والمطلوب {q4(qty)}.")
    total_value = sum((D(x["qty_remaining"])*D(x["unit_cost"]) for x in layers), D("0"))
    avg_cost = (total_value / available) if available else D("0")
    remaining, cost_total, allocations = qty, D("0"), []
    for layer in layers:
        if remaining <= 0: break
        layer_qty = D(layer["qty_remaining"])
        take = min(layer_qty, remaining)
        unit_cost = avg_cost if valuation_method == "average" else D(layer["unit_cost"])
        cost = money(take * unit_cost)
        new_remaining = layer_qty - take
        c.execute("UPDATE stock_valuation_layers SET qty_remaining=? WHERE id=?", (str(q4(new_remaining)), layer["id"]))
        allocations.append((layer["lot_id"], take, unit_cost, cost))
        cost_total += cost
        remaining -= take
    return money(cost_total), allocations


def make_lot(c, product, lot_number, production_date, expiry_date):
    if not product["track_lot"] and not product["track_expiry"]:
        return None
    if not lot_number:
        raise ValueError("هذا الصنف يتطلب رقم تشغيلة.")
    if product["track_expiry"] and not expiry_date:
        raise ValueError("تاريخ الصلاحية مطلوب لهذا الصنف.")
    if expiry_date:
        try:
            exp = date.fromisoformat(expiry_date)
            if exp < date.today():
                raise ValueError("لا يمكن استلام تشغيلة منتهية الصلاحية.")
        except ValueError as e:
            if "منتهية" in str(e): raise
            raise ValueError("تاريخ الصلاحية غير صحيح.")
    c.execute("""INSERT OR IGNORE INTO stock_lots(product_id,lot_number,production_date,expiry_date)
                 VALUES(?,?,?,?)""", (product["id"],lot_number,production_date or None,expiry_date or None))
    lot = c.execute("SELECT * FROM stock_lots WHERE product_id=? AND lot_number=?", (product["id"],lot_number)).fetchone()
    if lot["expiry_date"] != (expiry_date or None):
        raise ValueError("رقم التشغيلة موجود بتاريخ صلاحية مختلف.")
    return lot["id"]


@stock_bp.route("/")
@stock_permission("stock.view")
def index():
    c=get_db()
    products=c.execute("SELECT COUNT(*) n FROM stock_products WHERE active=1").fetchone()["n"]
    warehouses=c.execute("SELECT COUNT(*) n FROM stock_warehouses WHERE active=1").fetchone()["n"]
    rows=c.execute("""SELECT p.id,p.sku,p.name_ar,p.uom,
      COALESCE(SUM(CAST(l.qty_remaining AS REAL)),0) qty
      FROM stock_products p LEFT JOIN stock_valuation_layers l ON l.product_id=p.id
      WHERE p.active=1 GROUP BY p.id ORDER BY p.name_ar LIMIT 10""").fetchall()
    c.close()
    return render_template("stock_dashboard.html",products=products,warehouses=warehouses,rows=rows)


@stock_bp.route("/products", methods=["GET","POST"])
@stock_permission("stock.manage")
def products():
    c=get_db()
    if request.method=="POST":
        try:
            sku=request.form.get("sku","").strip()
            name=request.form.get("name_ar","").strip()
            uom=request.form.get("uom","قطعة").strip()
            if not sku or not name or not uom: raise ValueError("الكود والاسم ووحدة القياس حقول مطلوبة.")
            c.execute("""INSERT INTO stock_products(sku,name_ar,category,product_type,uom,barcode,track_lot,track_expiry,min_qty)
              VALUES(?,?,?,?,?,?,?,?,?)""",(sku,name,request.form.get("category","").strip(),
              request.form.get("product_type","goods"),uom,request.form.get("barcode","").strip() or None,
              int(request.form.get("track_lot")=="on"),int(request.form.get("track_expiry")=="on"),
              str(q4(dec(request.form.get("min_qty","0"),"الحد الأدنى")))))
            c.commit(); flash("تم حفظ الصنف.", "success")
        except (ValueError,sqlite3.IntegrityError) as e:
            c.rollback(); flash(str(e) if isinstance(e,ValueError) else "كود الصنف أو الباركود مستخدم بالفعل.", "error")
        finally: c.close()
        return redirect(url_for("stock.products"))
    rows=c.execute("SELECT * FROM stock_products ORDER BY sku").fetchall(); c.close()
    return render_template("stock_products.html",rows=rows)


@stock_bp.route("/warehouses", methods=["GET","POST"])
@stock_permission("stock.manage")
def warehouses():
    c=get_db()
    if request.method=="POST":
        try:
            code=request.form.get("code","").strip(); name=request.form.get("name_ar","").strip()
            if not code or not name: raise ValueError("كود واسم المخزن مطلوبان.")
            c.execute("INSERT INTO stock_warehouses(code,name_ar,branch) VALUES(?,?,?)",(code,name,request.form.get("branch","").strip()))
            c.commit(); flash("تم حفظ المخزن.", "success")
        except (ValueError,sqlite3.IntegrityError) as e:
            c.rollback(); flash(str(e) if isinstance(e,ValueError) else "كود المخزن مستخدم بالفعل.", "error")
        finally: c.close()
        return redirect(url_for("stock.warehouses"))
    rows=c.execute("SELECT * FROM stock_warehouses ORDER BY code").fetchall(); c.close()
    return render_template("stock_warehouses.html",rows=rows)


@stock_bp.route("/products/<int:product_id>/edit", methods=["GET","POST"])
@stock_permission("stock.manage")
def edit_product(product_id):
    c=get_db(); product=c.execute("SELECT * FROM stock_products WHERE id=?",(product_id,)).fetchone()
    if not product:
        c.close(); flash("الصنف غير موجود.","error"); return redirect(url_for("stock.products"))
    used=c.execute("SELECT 1 FROM stock_movements WHERE product_id=? LIMIT 1",(product_id,)).fetchone() is not None
    if request.method=="POST":
        try:
            sku=request.form.get("sku","").strip(); name=request.form.get("name_ar","").strip(); uom=request.form.get("uom","").strip()
            if not sku or not name or not uom: raise ValueError("الكود والاسم والوحدة مطلوبة.")
            c.execute("""UPDATE stock_products SET sku=?,name_ar=?,category=?,product_type=?,uom=?,barcode=?,track_lot=?,track_expiry=?,min_qty=? WHERE id=?""",
              (sku,name,request.form.get("category","").strip(),request.form.get("product_type","goods"),uom,request.form.get("barcode","").strip() or None,int(request.form.get("track_lot")=="on"),int(request.form.get("track_expiry")=="on"),str(q4(dec(request.form.get("min_qty","0"),"الحد الأدنى"))),product_id))
            c.commit(); c.close(); flash("تم تعديل الصنف.","success"); return redirect(url_for("stock.products"))
        except (ValueError,sqlite3.IntegrityError) as e:
            c.rollback(); flash(str(e) if isinstance(e,ValueError) else "الكود أو الباركود مستخدم بالفعل.","error")
    c.close(); return render_template("stock_product_edit.html",product=product,used=used)


@stock_bp.route("/products/<int:product_id>/delete",methods=["POST"])
@stock_permission("stock.manage")
def delete_product(product_id):
    c=get_db(); product=c.execute("SELECT * FROM stock_products WHERE id=?",(product_id,)).fetchone()
    if not product: flash("الصنف غير موجود.","error")
    else:
        refs=[("stock_movements","حركات المخزون"),("stock_lots","التشغيلات"),("stock_valuation_layers","طبقات التقييم"),("purchase_order_lines","أوامر الشراء"),("goods_receipt_lines","استلامات المشتريات"),("sales_invoice_lines","فواتير البيع")]
        used=next((label for table,label in refs if c.execute(f"SELECT 1 FROM {table} WHERE product_id=? LIMIT 1",(product_id,)).fetchone()),None)
        if used: flash("لا يمكن حذف الصنف لوجود ارتباط في "+used+".","error")
        else:
            try: c.execute("DELETE FROM stock_products WHERE id=?",(product_id,)); c.commit(); flash("تم حذف الصنف لعدم وجود حركات مرتبطة.","success")
            except sqlite3.IntegrityError: c.rollback(); flash("لا يمكن حذف الصنف لوجود مستند مرتبط به.","error")
    c.close(); return redirect(url_for("stock.products"))


@stock_bp.route("/warehouses/<int:warehouse_id>/edit",methods=["GET","POST"])
@stock_permission("stock.manage")
def edit_warehouse(warehouse_id):
    c=get_db(); warehouse=c.execute("SELECT * FROM stock_warehouses WHERE id=?",(warehouse_id,)).fetchone()
    if not warehouse:
        c.close(); flash("المخزن غير موجود.","error"); return redirect(url_for("stock.warehouses"))
    if request.method=="POST":
        try:
            code=request.form.get("code","").strip(); name=request.form.get("name_ar","").strip()
            if not code or not name: raise ValueError("كود واسم المخزن مطلوبان.")
            c.execute("UPDATE stock_warehouses SET code=?,name_ar=?,branch=? WHERE id=?",(code,name,request.form.get("branch","").strip(),warehouse_id))
            c.commit(); c.close(); flash("تم تعديل المخزن.","success"); return redirect(url_for("stock.warehouses"))
        except (ValueError,sqlite3.IntegrityError) as e: c.rollback(); flash(str(e) if isinstance(e,ValueError) else "كود المخزن مستخدم بالفعل.","error")
    c.close(); return render_template("stock_warehouse_edit.html",warehouse=warehouse)


@stock_bp.route("/warehouses/<int:warehouse_id>/delete",methods=["POST"])
@stock_permission("stock.manage")
def delete_warehouse(warehouse_id):
    c=get_db(); warehouse=c.execute("SELECT * FROM stock_warehouses WHERE id=?",(warehouse_id,)).fetchone()
    if not warehouse: flash("المخزن غير موجود.","error")
    else:
        refs=[("stock_movements","حركات المخزون"),("stock_valuation_layers","طبقات تقييم المخزون"),("goods_receipts","استلامات المشتريات"),("sales_invoices","فواتير البيع")]
        used=next((label for table,label in refs if c.execute(f"SELECT 1 FROM {table} WHERE warehouse_id=? OR source_warehouse_id=? OR target_warehouse_id=? LIMIT 1",(warehouse_id,warehouse_id,warehouse_id)).fetchone()),None)
        if used: flash("لا يمكن حذف المخزن لوجود "+used+" مرتبطة به.","error")
        else:
            try: c.execute("DELETE FROM stock_warehouses WHERE id=?",(warehouse_id,)); c.commit(); flash("تم حذف المخزن.","success")
            except sqlite3.IntegrityError: c.rollback(); flash("لا يمكن حذف المخزن لوجود مستند مرتبط به.","error")
    c.close(); return redirect(url_for("stock.warehouses"))


@stock_bp.route("/movements", methods=["GET","POST"])
@stock_permission("stock.manage")
def movements():
    c=get_db()
    if request.method=="POST":
        try:
            kind=request.form.get("movement_type")
            if kind not in {"receipt","issue","transfer","scrap"}: raise ValueError("نوع الحركة غير صحيح.")
            product=c.execute("SELECT * FROM stock_products WHERE id=? AND active=1",(request.form.get("product_id"),)).fetchone()
            if not product: raise ValueError("الصنف غير موجود أو غير نشط.")
            if product["product_type"] == "service": raise ValueError("الخدمات لا تُدار كأصناف مخزنية.")
            qty=q4(dec(request.form.get("quantity"),"الكمية"))
            if qty<=0: raise ValueError("الكمية يجب أن تكون أكبر من صفر.")
            src=request.form.get("source_warehouse_id",type=int)
            dst=request.form.get("target_warehouse_id",type=int)
            if kind=="receipt" and not dst: raise ValueError("حدد مخزن الاستلام.")
            if kind in {"issue","scrap","transfer"} and not src: raise ValueError("حدد مخزن الصرف.")
            if kind=="transfer" and (not dst or dst==src): raise ValueError("حدد مخزن وجهة مختلفًا عن المصدر.")
            c.execute("BEGIN IMMEDIATE")
            lot_id=None
            lot_no=request.form.get("lot_number","").strip()
            expiry=request.form.get("expiry_date","").strip()
            if kind=="receipt":
                lot_id=make_lot(c,product,lot_no,request.form.get("production_date"),expiry)
                unit_cost=dec(request.form.get("unit_cost","0"),"تكلفة الوحدة")
                if unit_cost<0: raise ValueError("التكلفة لا يمكن أن تكون سالبة.")
                total=money(qty*unit_cost)
            else:
                if product["track_lot"] or product["track_expiry"]:
                    if not lot_no: raise ValueError("أدخل رقم التشغيلة للصرف من صنف يتطلب تتبعًا.")
                    lot=c.execute("SELECT * FROM stock_lots WHERE product_id=? AND lot_number=?",(product["id"],lot_no)).fetchone()
                    if not lot: raise ValueError("التشغيلة غير مسجلة.")
                    if lot["expiry_date"] and date.fromisoformat(lot["expiry_date"])<date.today(): raise ValueError("لا يمكن صرف تشغيلة منتهية الصلاحية.")
                    lot_id=lot["id"]
                method_row=c.execute("SELECT setting_value FROM company_settings WHERE setting_key='inventory_cost_method'").fetchone()
                method=method_row["setting_value"] if method_row else "average"
                total,allocations=consume_layers(c,product["id"],src,qty,method,lot_id)
                unit_cost=(total/qty).quantize(MONEY,rounding=ROUND_HALF_UP)
            movement_no=f"STK-{date.today().strftime('%Y%m%d')}-{c.execute('SELECT COALESCE(MAX(id),0)+1 n FROM stock_movements').fetchone()['n']:06d}"
            movement_date=request.form.get("movement_date") or date.today().isoformat()
            if kind=="transfer":
                # Validate and consume source layers, then recreate equivalent valuation layers in destination.
                for allocated_lot,allocated_qty,allocated_cost,allocated_total in allocations:
                    pass
            issue_purpose=request.form.get("issue_purpose") if kind=="issue" else None
            if kind=="issue" and issue_purpose not in {"sale","internal"}:
                raise ValueError("حدد غرض الصرف: تكلفة مبيعات أو استخدام داخلي.")
            cur=c.execute("""INSERT INTO stock_movements(movement_no,movement_type,product_id,source_warehouse_id,target_warehouse_id,
              lot_id,quantity,unit_cost,total_cost,movement_date,notes,created_by,issue_purpose)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",(movement_no,kind,product["id"],src,dst,lot_id,str(qty),str(unit_cost),str(total),movement_date,request.form.get("notes","").strip(),session.get("user_id"),issue_purpose))
            mid=cur.lastrowid
            if kind=="receipt":
                c.execute("""INSERT INTO stock_valuation_layers(product_id,warehouse_id,lot_id,source_movement_id,qty_in,qty_remaining,unit_cost)
                  VALUES(?,?,?,?,?,?,?)""",(product["id"],dst,lot_id,mid,str(qty),str(qty),str(unit_cost)))
            elif kind=="transfer":
                for allocated_lot,allocated_qty,allocated_cost,allocated_total in allocations:
                    c.execute("""INSERT INTO stock_valuation_layers(product_id,warehouse_id,lot_id,source_movement_id,qty_in,qty_remaining,unit_cost)
                      VALUES(?,?,?,?,?,?,?)""",(product["id"],dst,allocated_lot,mid,str(allocated_qty),str(allocated_qty),str(allocated_cost)))
            if kind=="scrap":
                post_scrap_journal(c,mid,movement_no,product,movement_date,total,session.get("user_id"))
            elif kind=="transfer":
                # One consolidated inventory control account: internal transfers do not affect GL.
                c.execute("UPDATE stock_movements SET accounting_status='not_required' WHERE id=?", (mid,))
            elif kind=="receipt":
                # A receipt needs a supplier invoice or an explicit GRNI document before posting.
                c.execute("UPDATE stock_movements SET accounting_status='pending_document' WHERE id=?", (mid,))
            elif kind=="issue":
                post_issue_journal(c,mid,movement_no,product,movement_date,total,session.get("user_id"),issue_purpose)
            c.commit(); flash(f"تم ترحيل الحركة {movement_no}.", "success")
        except (ValueError,InvalidOperation,sqlite3.IntegrityError) as e:
            c.rollback(); flash(str(e), "error")
        finally: c.close()
        return redirect(url_for("stock.movements"))
    products=c.execute("SELECT id,sku,name_ar,uom,track_lot,track_expiry FROM stock_products WHERE active=1 ORDER BY name_ar").fetchall()
    warehouses=c.execute("SELECT id,code,name_ar FROM stock_warehouses WHERE active=1 ORDER BY code").fetchall()
    rows=c.execute("""SELECT m.*,p.sku,p.name_ar,s.name_ar source_name,t.name_ar target_name
      FROM stock_movements m JOIN stock_products p ON p.id=m.product_id
      LEFT JOIN stock_warehouses s ON s.id=m.source_warehouse_id LEFT JOIN stock_warehouses t ON t.id=m.target_warehouse_id
      ORDER BY m.id DESC LIMIT 30""").fetchall()
    c.close()
    return render_template("stock_movements.html",products=products,warehouses=warehouses,rows=rows,today=date.today().isoformat())


@stock_bp.route("/reports")
@stock_permission("stock.view")
def reports():
    c=get_db()
    rows=c.execute("""SELECT p.id,p.sku,p.name_ar,p.uom,
      COALESCE(SUM(CAST(l.qty_remaining AS REAL)),0) qty,
      COALESCE(SUM(CAST(l.qty_remaining AS REAL)*CAST(l.unit_cost AS REAL)),0) value
      FROM stock_products p LEFT JOIN stock_valuation_layers l ON l.product_id=p.id
      WHERE p.active=1 GROUP BY p.id ORDER BY p.sku""").fetchall()
    lots=c.execute("""SELECT p.sku,p.name_ar,l.lot_number,l.expiry_date,
      COALESCE(SUM(CAST(v.qty_remaining AS REAL)),0) qty
      FROM stock_lots l JOIN stock_products p ON p.id=l.product_id
      LEFT JOIN stock_valuation_layers v ON v.lot_id=l.id
      WHERE l.expiry_date IS NOT NULL GROUP BY l.id ORDER BY l.expiry_date""").fetchall()
    movements=c.execute("""SELECT m.*,p.sku,p.name_ar,w.name_ar warehouse
      FROM stock_movements m JOIN stock_products p ON p.id=m.product_id
      LEFT JOIN stock_warehouses w ON w.id=COALESCE(m.target_warehouse_id,m.source_warehouse_id)
      ORDER BY m.id DESC LIMIT 100""").fetchall()
    c.close()
    return render_template("stock_reports.html",rows=rows,lots=lots,movements=movements)


@stock_bp.route("/barcode")
@stock_permission("stock.view")
def barcode_lookup():
    code=request.args.get("q","").strip()
    c=get_db()
    product=c.execute("SELECT * FROM stock_products WHERE barcode=? OR sku=?",(code,code)).fetchone() if code else None
    c.close()
    return render_template("stock_barcode.html",product=product,code=code)
